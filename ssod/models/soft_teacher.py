from matplotlib.colors import same_color
import torch
from mmcv.runner.fp16_utils import force_fp32
from mmdet.core import bbox2roi, multi_apply
from mmdet.models import DETECTORS, build_detector, losses
from torch._C import device
import torch.nn.functional as F

from ssod.utils.structure_utils import dict_split, weighted_loss
from ssod.utils import log_image_with_boxes, log_every_n

from .multi_stream_detector import MultiSteamDetector
from .utils import Transform2D, filter_invalid


@DETECTORS.register_module()
class SoftTeacher(MultiSteamDetector):
    def __init__(self, model: dict, train_cfg=None, test_cfg=None, memory_k=65536, ctr1_T=0.2, ctr2_T=0.2,
     ctr1_lam_sup=0.1, ctr1_lam_unsup=0.1, ctr2_lam_sup=0.1, ctr2_lam_unsup=0.1, ctr2_num=2):
        super(SoftTeacher, self).__init__(
            dict(teacher=build_detector(model), student=build_detector(model)),
            train_cfg=train_cfg,
            test_cfg=test_cfg,
        )
        if train_cfg is not None:
            self.freeze("teacher")
            self.unsup_weight = self.train_cfg.unsup_weight
        
        self.memory_k = memory_k
        self.ctr1_T = ctr1_T
        self.ctr2_T = ctr2_T
        self.ctr1_lam_sup = ctr1_lam_sup
        self.ctr1_lam_unsup = ctr1_lam_unsup
        self.ctr2_lam_sup = ctr2_lam_sup
        self.ctr2_lam_unsup = ctr2_lam_unsup
        self.projector_dim = model.projector_dim
        self.ctr2_num = ctr2_num
        self.register_buffer("queue_vector", torch.randn(memory_k, model.projector_dim)) 
        self.queue_vector = F.normalize(self.queue_vector, dim=1)

        self.register_buffer("queue_ptr", torch.zeros(1, dtype=torch.long))


    def forward_train(self, img, img_metas, **kwargs):
        super().forward_train(img, img_metas, **kwargs)
        kwargs.update({"img": img})
        kwargs.update({"img_metas": img_metas})
        kwargs.update({"tag": [meta["tag"] for meta in img_metas]})
        data_groups = dict_split(kwargs, "tag")
        for _, v in data_groups.items():
            v.pop("tag")

        loss = {}
        #! Warnings: By splitting losses for supervised data and unsupervised data with different names,
        #! it means that at least one sample for each group should be provided on each gpu.
        #! In some situation, we can only put one image per gpu, we have to return the sum of loss
        #! and log the loss with logger instead. Or it will try to sync tensors don't exist.
        if "sup" in data_groups:
            gt_bboxes = data_groups["sup"]["gt_bboxes"]
            log_every_n(
                {"sup_gt_num": sum([len(bbox) for bbox in gt_bboxes]) / len(gt_bboxes)}
            )
            sup_loss = self.student.forward_train(**data_groups["sup"])
            sup_ctr_loss = self.ctr_loss(data_groups["ctr_anchor_sup"], data_groups["ctr_ctr_sup"])
            sup_ctr_loss['ctr1_loss'] = sup_ctr_loss['ctr1_loss'] * self.ctr1_lam_sup
            sup_ctr_loss['ctr2_loss'] = sup_ctr_loss['ctr2_loss'] * self.ctr2_lam_sup
            sup_loss = {"sup_" + k: v for k, v in sup_loss.items()}
            sup_ctr_loss = {"sup_" + k: v for k, v in sup_ctr_loss.items()}
            loss.update(**sup_loss)
            loss.update(**sup_ctr_loss)
        if "unsup_student" in data_groups:
            unsup_loss = weighted_loss(
                self.foward_unsup_train(
                    data_groups["unsup_teacher"], data_groups["unsup_student"], data_groups['ctr_anchor_unsup'], data_groups['ctr_ctr_unsup']
                ),
                weight=self.unsup_weight,
            )
            unsup_loss = {"unsup_" + k: v for k, v in unsup_loss.items()}
            loss.update(**unsup_loss)

        return loss

    def foward_unsup_train(self, teacher_data, student_data, anchor_data, ctr_data):
        # sort the teacher and student input to avoid some bugs
        tnames = [meta["filename"] for meta in teacher_data["img_metas"]]
        snames = [meta["filename"] for meta in student_data["img_metas"]]
        tidx = [tnames.index(name) for name in snames]
        with torch.no_grad():
            teacher_info = self.extract_teacher_info(
                teacher_data["img"][
                    torch.Tensor(tidx).to(teacher_data["img"].device).long()
                ],
                [teacher_data["img_metas"][idx] for idx in tidx],
                [teacher_data["proposals"][idx] for idx in tidx]
                if ("proposals" in teacher_data)
                and (teacher_data["proposals"] is not None)
                else None,
            )
        student_info = self.extract_student_info(**student_data)

        losses = dict()

        ctr_loss = self.foward_unsup_ctr_train(anchor_data, ctr_data)
        pseudo_loss = self.compute_pseudo_label_loss(student_info, teacher_info)

        losses.update(**ctr_loss)
        losses.update(**pseudo_loss)

        return losses
    
    def foward_unsup_ctr_train(self, anchor_data, ctr_data):
        tnames = [meta["filename"] for meta in ctr_data["img_metas"]]
        snames = [meta["filename"] for meta in anchor_data["img_metas"]]
        tidx = [tnames.index(name) for name in snames]
        with torch.no_grad():
            ctr_info = self.extract_teacher_info(
                ctr_data["img"][
                    torch.Tensor(tidx).to(ctr_data["img"].device).long()
                ],
                [ctr_data["img_metas"][idx] for idx in tidx],
                [ctr_data["proposals"][idx] for idx in tidx]
                if ("proposals" in ctr_data)
                and (ctr_data["proposals"] is not None)
                else None,
            )

        ctr_bboxes, ctr_labels, _ = multi_apply(
            filter_invalid,
            [bbox[:, :4] for bbox in ctr_info['det_bboxes']],
            ctr_info['det_labels'],
            [bbox[:, 4] for bbox in ctr_info['det_bboxes']],
            thr=self.train_cfg.cls_pseudo_threshold,
            min_size=self.train_cfg.min_pseduo_box_size
        )

        stumatrix = [
            torch.from_numpy(meta["transform_matrix"]).float().to(anchor_data["img"].device)
            for meta in anchor_data["img_metas"]
        ]

        M = self._get_trans_mat(
            ctr_info["transform_matrix"], stumatrix
        )

        anchor_bboxes = self._transform_bbox(
            ctr_bboxes,
            M,
            [meta["img_shape"] for meta in anchor_data["img_metas"]],
        )

        anchor_labels = [ctr_l.clone().detach() for ctr_l in ctr_labels]

        anchor_data['gt_bboxes'] = anchor_bboxes
        anchor_data['gt_labels'] = anchor_labels
        ctr_data['gt_bboxes'] = ctr_bboxes
        ctr_data['gt_labels'] = ctr_labels

        return self.ctr_loss(anchor_data, ctr_data)


    def compute_pseudo_label_loss(self, student_info, teacher_info):
        M = self._get_trans_mat(
            teacher_info["transform_matrix"], student_info["transform_matrix"]
        )

        pseudo_bboxes = self._transform_bbox(
            teacher_info["det_bboxes"],
            M,
            [meta["img_shape"] for meta in student_info["img_metas"]],
        )
        pseudo_labels = teacher_info["det_labels"]
        loss = {}
        rpn_loss, proposal_list = self.rpn_loss(
            student_info["rpn_out"],
            pseudo_bboxes,
            student_info["img_metas"],
            student_info=student_info,
        )
        loss.update(rpn_loss)
        if proposal_list is not None:
            student_info["proposals"] = proposal_list
        if self.train_cfg.use_teacher_proposal:
            proposals = self._transform_bbox(
                teacher_info["proposals"],
                M,
                [meta["img_shape"] for meta in student_info["img_metas"]],
            )
        else:
            proposals = student_info["proposals"]

        loss.update(
            self.unsup_rcnn_loss(
                student_info["backbone_feature"],
                student_info["img_metas"],
                proposals,
                pseudo_bboxes,
                pseudo_labels,
                student_info=student_info,
            )
        )
        return loss

    def rpn_loss(
        self,
        rpn_out,
        pseudo_bboxes,
        img_metas,
        gt_bboxes_ignore=None,
        student_info=None,
        **kwargs,
    ):
        if self.student.with_rpn:
            gt_bboxes = []
            for bbox in pseudo_bboxes:
                bbox, _, _ = filter_invalid(
                    bbox[:, :4],
                    score=bbox[
                        :, 4
                    ],  # TODO: replace with foreground score, here is classification score,
                    thr=self.train_cfg.rpn_pseudo_threshold,
                    min_size=self.train_cfg.min_pseduo_box_size,
                )
                gt_bboxes.append(bbox)
            log_every_n(
                {"rpn_gt_num": sum([len(bbox) for bbox in gt_bboxes]) / len(gt_bboxes)}
            )
            loss_inputs = rpn_out + [[bbox.float() for bbox in gt_bboxes], img_metas]
            losses = self.student.rpn_head.loss(
                *loss_inputs, gt_bboxes_ignore=gt_bboxes_ignore
            )
            proposal_cfg = self.student.train_cfg.get(
                "rpn_proposal", self.student.test_cfg.rpn
            )
            proposal_list = self.student.rpn_head.get_bboxes(
                *rpn_out, img_metas=img_metas, cfg=proposal_cfg
            )
            log_image_with_boxes(
                "rpn",
                student_info["img"][0],
                pseudo_bboxes[0][:, :4],
                bbox_tag="rpn_pseudo_label",
                scores=pseudo_bboxes[0][:, 4],
                interval=500,
                img_norm_cfg=student_info["img_metas"][0]["img_norm_cfg"],
            )
            return losses, proposal_list
        else:
            return {}, None

    def unsup_rcnn_loss(
        self,
        feat,
        img_metas,
        proposal_list,
        pseudo_bboxes,
        pseudo_labels,
        student_info=None,
        **kwargs,
    ):
        gt_bboxes, gt_labels, _ = multi_apply(
            filter_invalid,
            [bbox[:, :4] for bbox in pseudo_bboxes],
            pseudo_labels,
            [bbox[:, 4] for bbox in pseudo_bboxes],
            thr=self.train_cfg.cls_pseudo_threshold,
            min_size=self.train_cfg.min_pseduo_box_size
        )
        log_every_n(
            {"rcnn_gt_num": sum([len(bbox) for bbox in gt_bboxes]) / len(gt_bboxes)}
        )
        losses = self.student.roi_head.forward_train(
            feat, img_metas, proposal_list, gt_bboxes, gt_labels, **kwargs
        )
        if len(gt_bboxes[0]) > 0:
            log_image_with_boxes(
                "rcnn",
                student_info["img"][0],
                gt_bboxes[0],
                bbox_tag="pseudo_label",
                labels=gt_labels[0],
                class_names=self.CLASSES,
                interval=500,
                img_norm_cfg=student_info["img_metas"][0]["img_norm_cfg"],
            )
        return losses

    def get_sampling_result(
        self,
        img_metas,
        proposal_list,
        gt_bboxes,
        gt_labels,
        gt_bboxes_ignore=None,
        mode='student',
        **kwargs,
    ):
        num_imgs = len(img_metas)
        if gt_bboxes_ignore is None:
            gt_bboxes_ignore = [None for _ in range(num_imgs)]
        sampling_results = []
        if mode == 'student':
            for i in range(num_imgs):
                assign_result = self.student.roi_head.bbox_assigner.assign(
                    proposal_list[i], gt_bboxes[i], gt_bboxes_ignore[i], gt_labels[i]
                )
                sampling_result = self.student.roi_head.bbox_sampler.sample(
                    assign_result,
                    proposal_list[i],
                    gt_bboxes[i],
                    gt_labels[i],
                )
                sampling_results.append(sampling_result)
        else:
            for i in range(num_imgs):
                assign_result = self.teacher.roi_head.bbox_assigner.assign(
                    proposal_list[i], gt_bboxes[i], gt_bboxes_ignore[i], gt_labels[i]
                )
                sampling_result = self.teacher.roi_head.bbox_sampler.sample(
                    assign_result,
                    proposal_list[i],
                    gt_bboxes[i],
                    gt_labels[i],
                )
                sampling_results.append(sampling_result)
        return sampling_results

    @force_fp32(apply_to=["bboxes", "trans_mat"])
    def _transform_bbox(self, bboxes, trans_mat, max_shape):
        bboxes = Transform2D.transform_bboxes(bboxes, trans_mat, max_shape)
        return bboxes

    @force_fp32(apply_to=["a", "b"])
    def _get_trans_mat(self, a, b):
        return [bt @ at.inverse() for bt, at in zip(b, a)]

    def extract_student_info(self, img, img_metas, proposals=None, **kwargs):
        student_info = {}
        student_info["img"] = img
        feat = self.student.extract_feat(img)
        student_info["backbone_feature"] = feat
        if self.student.with_rpn:
            rpn_out = self.student.rpn_head(feat)
            student_info["rpn_out"] = list(rpn_out)
        student_info["img_metas"] = img_metas
        student_info["proposals"] = proposals
        student_info["transform_matrix"] = [
            torch.from_numpy(meta["transform_matrix"]).float().to(feat[0][0].device)
            for meta in img_metas
        ]
        return student_info

    def extract_teacher_info(self, img, img_metas, proposals=None, **kwargs):
        teacher_info = {}
        feat = self.teacher.extract_feat(img)
        teacher_info["backbone_feature"] = feat
        if proposals is None:
            proposal_cfg = self.teacher.train_cfg.get(
                "rpn_proposal", self.teacher.test_cfg.rpn
            )
            rpn_out = list(self.teacher.rpn_head(feat))
            proposal_list = self.teacher.rpn_head.get_bboxes(
                *rpn_out, img_metas=img_metas, cfg=proposal_cfg
            )
        else:
            proposal_list = proposals
        teacher_info["proposals"] = proposal_list

        proposal_list, proposal_label_list = self.teacher.roi_head.simple_test_bboxes(
            feat, img_metas, proposal_list, self.teacher.test_cfg.rcnn, rescale=False
        )

        proposal_list = [p.to(feat[0].device) for p in proposal_list]
        proposal_list = [
            p if p.shape[0] > 0 else p.new_zeros(0, 5) for p in proposal_list
        ]
        proposal_label_list = [p.to(feat[0].device) for p in proposal_label_list]
        # filter invalid box roughly
        if isinstance(self.train_cfg.pseudo_label_initial_score_thr, float):
            thr = self.train_cfg.pseudo_label_initial_score_thr
        else:
            # TODO: use dynamic threshold
            raise NotImplementedError("Dynamic Threshold is not implemented yet.")
        proposal_list, proposal_label_list, _ = list(
            zip(
                *[
                    filter_invalid(
                        proposal,
                        proposal_label,
                        proposal[:, -1],
                        thr=thr,
                        min_size=self.train_cfg.min_pseduo_box_size,
                    )
                    for proposal, proposal_label in zip(
                        proposal_list, proposal_label_list
                    )
                ]
            )
        )

        det_bboxes = proposal_list
        det_labels = proposal_label_list
        teacher_info["det_bboxes"] = det_bboxes
        teacher_info["det_labels"] = det_labels
        teacher_info["transform_matrix"] = [
            torch.from_numpy(meta["transform_matrix"]).float().to(feat[0][0].device)
            for meta in img_metas
        ]
        teacher_info["img_metas"] = img_metas
        return teacher_info

    @staticmethod
    def aug_box(boxes, times=1, frac=0.06):
        def _aug_single(box):
            # random translate
            # TODO: random flip or something
            box_scale = box[:, 2:4] - box[:, :2]
            box_scale = (
                box_scale.clamp(min=1)[:, None, :].expand(-1, 2, 2).reshape(-1, 4)
            )
            aug_scale = box_scale * frac  # [n,4]

            offset = (
                torch.randn(times, box.shape[0], 4, device=box.device)
                * aug_scale[None, ...]
            )
            new_box = box.clone()[None, ...].expand(times, box.shape[0], -1)
            return torch.cat(
                [new_box[:, :, :4].clone() + offset, new_box[:, :, 4:]], dim=-1
            )

        return [_aug_single(box) for box in boxes]

    def _load_from_state_dict(
        self,
        state_dict,
        prefix,
        local_metadata,
        strict,
        missing_keys,
        unexpected_keys,
        error_msgs,
    ):
        if not any(["student" in key or "teacher" in key for key in state_dict.keys()]):
            keys = list(state_dict.keys())
            state_dict.update({"teacher." + k: state_dict[k] for k in keys})
            state_dict.update({"student." + k: state_dict[k] for k in keys})
            for k in keys:
                state_dict.pop(k)

        return super()._load_from_state_dict(
            state_dict,
            prefix,
            local_metadata,
            strict,
            missing_keys,
            unexpected_keys,
            error_msgs,
        )

    @torch.no_grad()
    def concat_all_gather(self, features):
        """
        Performs all_gather operation on the provided tensors.
        *** Warning ***: torch.distributed.all_gather has no gradient.
        """
        device = features.device
        local_batch = torch.tensor(features.size(0)).to(device)
        batch_size_gather = [torch.ones((1)).to(device)
            for _ in range(torch.distributed.get_world_size())]
        torch.distributed.all_gather(batch_size_gather, local_batch.float(), async_op=False)
        
        batch_size_gather = [int(bs.item()) for bs in batch_size_gather]

        max_batch = max(batch_size_gather)
        size = (max_batch, features.size(1))
        temp_features = torch.zeros(max_batch - local_batch, features.size(1)).to(device)
        features = torch.cat([features, temp_features])

        # size = (int(tensors_gather[0].item()), features.size(1))
        # (int(tensors_gather[i].item()), features.size(1))
        features_gather = [torch.ones(size).to(device)
            for _ in range(torch.distributed.get_world_size())]
        torch.distributed.all_gather(features_gather, features, async_op=False)

        features_gather = [f[:bs, :] for bs, f in zip(batch_size_gather, features_gather)]

        features = torch.cat(features_gather, dim=0)

        return features

    @torch.no_grad()
    def _dequeue_and_enqueue(self, features):
        # gather keys before updating queue
        
        features = self.concat_all_gather(features)

        batch_size = features.size(0)

        if batch_size == 0:
            return

        assert features.size(1) == self.projector_dim

        ptr = int(self.queue_ptr)

        # replace the keys at ptr (dequeue and enqueue)
        if ptr + batch_size >= self.memory_k:
            redundant = ptr + batch_size - self.memory_k
            self.queue_vector[ptr:self.memory_k, :] = features.view(batch_size, -1)[:batch_size - redundant]
            self.queue_vector[:redundant, :] = features.view(batch_size, -1)[batch_size - redundant:]
        else:
            self.queue_vector[ptr:ptr + batch_size, :] = features.view(batch_size, -1)
        ptr = (ptr + batch_size) % self.memory_k  # move pointer

        self.queue_ptr[0] = ptr

    def extract_ctr_info(self, anchor_data, ctr_data):
        anchor_info = {}
        anchor_info["img"] = anchor_data['img']
        feat = self.student.extract_feat(anchor_data['img'])
        anchor_info["backbone_feature"] = feat
        if self.student.with_rpn:
            rpn_out = self.student.rpn_head(feat)
            anchor_info["rpn_out"] = list(rpn_out)
        anchor_info["img_metas"] = anchor_data["img_metas"]
        proposal_cfg = self.student.train_cfg.get(
            "rpn_proposal", self.student.test_cfg.rpn
        )
        proposal_list = self.student.rpn_head.get_bboxes(
            *rpn_out, img_metas=anchor_data["img_metas"], cfg=proposal_cfg
        )
        anchor_info["proposals"] = proposal_list
        anchor_info["transform_matrix"] = [
            torch.from_numpy(meta["transform_matrix"]).float().to(feat[0][0].device)
            for meta in anchor_data["img_metas"]
        ]
        anchor_info['sampling_result'] = self.get_sampling_result(
            anchor_data["img_metas"],
            anchor_info["proposals"],
            anchor_data['gt_bboxes'],
            anchor_data['gt_labels'],
            mode='student'
        )

        ctr_info = {}
        ctr_info["img"] = ctr_data['img']
        feat = self.teacher.extract_feat(ctr_data['img'])
        ctr_info["backbone_feature"] = feat
        if self.teacher.with_rpn:
            rpn_out = self.teacher.rpn_head(feat)
            ctr_info["rpn_out"] = list(rpn_out)
        ctr_info["img_metas"] = ctr_data["img_metas"]
        proposal_cfg = self.teacher.train_cfg.get(
            "rpn_proposal", self.teacher.test_cfg.rpn
        )
        proposal_list = self.teacher.rpn_head.get_bboxes(
            *rpn_out, img_metas=ctr_data["img_metas"], cfg=proposal_cfg
        )
        ctr_info["proposals"] = proposal_list
        ctr_info["transform_matrix"] = [
            torch.from_numpy(meta["transform_matrix"]).float().to(feat[0][0].device)
            for meta in ctr_data["img_metas"]
        ]
        ctr_info['sampling_result'] = self.get_sampling_result(
            ctr_data["img_metas"],
            ctr_info["proposals"],
            ctr_data['gt_bboxes'],
            ctr_data['gt_labels'],
            mode='teacher'
        )

        return anchor_info, ctr_info

    def ctr_loss(self, anchor_data, ctr_data):
        device = anchor_data['img'].device
        losses = dict()

        assert len(anchor_data['gt_bboxes']) == len(ctr_data['gt_bboxes']) == len(anchor_data['gt_labels']) == len(ctr_data['gt_labels']) == anchor_data['img'].size(0) == ctr_data['img'].size(0)

        gt_num = 0
        for i in range(len(anchor_data['gt_bboxes'])):
            gt_num += anchor_data['gt_bboxes'][i].size(0)
        if gt_num == 0:
            losses['ctr1_loss'] = torch.zeros([1]).to(device)
            losses['ctr2_loss'] = torch.zeros([1]).to(device)
            self._dequeue_and_enqueue(torch.zeros([0, 128]).to(device))
            return losses
        
        valid_anchor_data = dict(gt_bboxes=[], gt_labels=[], img_metas=[])
        valid_ctr_data = dict(gt_bboxes=[], gt_labels=[], img_metas=[])
        valid_ind = []
        for i in range(len(anchor_data['gt_bboxes'])):
            assert anchor_data['gt_bboxes'][i].size(0) == anchor_data['gt_labels'][i].size(0) == ctr_data['gt_bboxes'][i].size(0) == ctr_data['gt_labels'][i].size(0)
            if anchor_data['gt_bboxes'][i].size(0) != 0:
                valid_anchor_data['gt_bboxes'].append(anchor_data['gt_bboxes'][i])
                valid_anchor_data['gt_labels'].append(anchor_data['gt_labels'][i])
                valid_anchor_data['img_metas'].append(anchor_data['img_metas'][i])
                valid_ctr_data['gt_bboxes'].append(ctr_data['gt_bboxes'][i])
                valid_ctr_data['gt_labels'].append(ctr_data['gt_labels'][i])
                valid_ctr_data['img_metas'].append(ctr_data['img_metas'][i])
                valid_ind.append(i)
        valid_anchor_data['img'] = anchor_data['img'][valid_ind]
        valid_ctr_data['img'] = ctr_data['img'][valid_ind]

        anchor_data = valid_anchor_data
        ctr_data = valid_ctr_data

        assert len(anchor_data['gt_bboxes']) == len(ctr_data['gt_bboxes']) == len(anchor_data['gt_labels']) == len(ctr_data['gt_labels']) == anchor_data['img'].size(0) == ctr_data['img'].size(0)

        anchor_info, ctr_info = self.extract_ctr_info(anchor_data, ctr_data)
        ctr1_loss = self.ctr_loss_1(anchor_info, ctr_info)
        ctr2_loss = self.ctr_loss_2(anchor_info, anchor_data['gt_bboxes'], anchor_data['gt_labels'])
        losses.update(**ctr1_loss)
        losses.update(**ctr2_loss)
        return losses

    def ctr_loss_1(self, anchor_info, ctr_info):
        student_feat = anchor_info['backbone_feature']
        teacher_feat = ctr_info['backbone_feature']
        losses = dict()
        device = student_feat[0].device

        anchor_sample_res = anchor_info['sampling_result']
        ctr_sample_res = ctr_info['sampling_result']

        batch = anchor_sample_res[0].bboxes.size(0)

        pos_gt_map_anchor = torch.zeros([0]).to(device).long()
        pos_gt_map_ctr = torch.zeros([0]).to(device).long()
        pos_labels_anchor = torch.zeros([0]).to(device).long()
        pos_labels_ctr = torch.zeros([0]).to(device).long()
        anchor_proposal = []
        ctr_proposal = []
        for i, (res_anchor, res_ctr) in enumerate(zip(anchor_sample_res, ctr_sample_res)):
            assert res_anchor.pos_assigned_gt_inds.size(0) == res_anchor.pos_gt_labels.size(0) == res_anchor.pos_bboxes.size(0) != 0
            assert res_ctr.pos_assigned_gt_inds.size(0) == res_ctr.pos_gt_labels.size(0) == res_ctr.pos_bboxes.size(0) != 0
            pos_gt_map_anchor = torch.cat([pos_gt_map_anchor, (res_anchor.pos_assigned_gt_inds + (i * batch)).view(-1)])
            pos_gt_map_ctr = torch.cat([pos_gt_map_ctr, (res_ctr.pos_assigned_gt_inds + (i * batch)).view(-1)])
            pos_labels_anchor = torch.cat([pos_labels_anchor, res_anchor.pos_gt_labels])
            pos_labels_ctr = torch.cat([pos_labels_ctr, res_ctr.pos_gt_labels])
            anchor_proposal.append(res_anchor.pos_bboxes)
            ctr_proposal.append(res_ctr.pos_bboxes)

        anchor_proposal = bbox2roi(anchor_proposal)
        ctr_proposal = bbox2roi(ctr_proposal)
        assert pos_gt_map_anchor.size(0) == pos_labels_anchor.size(0) == anchor_proposal.size(0)
        assert pos_gt_map_ctr.size(0) == pos_labels_ctr.size(0) == ctr_proposal.size(0)
        ctr_select_proposal = torch.zeros([0, 5]).to(device)
        for gt_map in pos_gt_map_anchor:
            pos_inds = pos_gt_map_ctr == gt_map
            pos_proposal = ctr_proposal[pos_inds]
            rand_index = torch.randint(low=0, high=pos_proposal.size(0), size=(1,))
            ctr_select_proposal = torch.cat([ctr_select_proposal, pos_proposal[rand_index]], dim=0)

        assert anchor_proposal.size(0) == ctr_select_proposal.size(0) and anchor_proposal.size(1) == ctr_select_proposal.size(1) == 5

        student_proposal_rois = anchor_proposal
        student_proposals = self.student.roi_head.bbox_roi_extractor(student_feat[:self.student.roi_head.bbox_roi_extractor.num_inputs], student_proposal_rois)
        teacher_proposal_rois = ctr_select_proposal
        teacher_proposals = self.teacher.roi_head.bbox_roi_extractor(teacher_feat[:self.teacher.roi_head.bbox_roi_extractor.num_inputs], teacher_proposal_rois)
        if student_proposals.size(0) == 0 or teacher_proposals.size(0) == 0:
            losses['ctr1_loss'] = torch.zeros([1]).to(device)
            self._dequeue_and_enqueue(torch.zeros([0, 128]).to(device))
            return losses 
        student_vec = self.student.projector(student_proposals.view(student_proposals.size(0), -1))
        student_vec = F.normalize(student_vec, dim=1)
        teacher_vec = self.teacher.projector(teacher_proposals.view(teacher_proposals.size(0), -1))
        teacher_vec = F.normalize(teacher_vec, dim=1)

        neg_logits = torch.einsum('nc,kc->nk', [student_vec, self.queue_vector.clone().detach()])
        pos_logits = torch.einsum('nc,nc->n', [student_vec, teacher_vec])
        logits = torch.cat([pos_logits[:, None], neg_logits], dim=1)
        logits /= self.ctr1_T
        labels = torch.zeros(logits.shape[0], dtype=torch.long).cuda()
        losses['ctr1_loss'] = F.cross_entropy(logits, labels)

        self._dequeue_and_enqueue(teacher_vec)

        return losses
        


    def ctr_loss_2(self, anchor_info, bboxes, labels):
        losses = dict()
        device = bboxes[0].device
        assert len(bboxes) == len(labels)
        for box_i in range(len(bboxes)):
            assert bboxes[box_i].size(0) == labels[box_i].size(0) != 0
            if bboxes[box_i].size(0) > self.ctr2_num:
                rand_index = torch.randint(low=0, high=bboxes[box_i].size(0), size=(self.ctr2_num,))
                bboxes[box_i] = bboxes[box_i][rand_index]
                labels[box_i] = labels[box_i][rand_index]
        
        student_proposal_rois = bbox2roi(bboxes)
        student_proposals = self.student.roi_head.bbox_roi_extractor(anchor_info['backbone_feature'][:self.student.roi_head.bbox_roi_extractor.num_inputs], student_proposal_rois)
        assert student_proposals.size(0) != 0
        student_vec = self.student.projector(student_proposals.view(student_proposals.size(0), -1))
        student_vec = F.normalize(student_vec, dim=1)
        all_labels = torch.cat(labels)

        assert student_proposal_rois.size(0) == all_labels.size(0)

        teacher_vec = torch.zeros([0, self.projector_dim]).to(device)
        for label in all_labels:
            same_label_item = self.labeled_dataset.get_same_label_item(label)
            assert isinstance(same_label_item, (list)) and len(same_label_item) == 3
            same_label_item = same_label_item[-1]
            while label not in same_label_item['gt_labels'].data.to(device):
                same_label_item = self.labeled_dataset.get_same_label_item(label)
            feat = self.teacher.extract_feat(same_label_item['img'].data.to(device)[None, :, :, :])
            teacher_proposal_rois = bbox2roi([same_label_item['gt_bboxes'].data[same_label_item['gt_labels'].data.to(device) == label].to(device)])
            rand_index = torch.randint(low=0, high=teacher_proposal_rois.size(0), size=(1,))
            teacher_proposal = self.teacher.roi_head.bbox_roi_extractor(feat[:self.teacher.roi_head.bbox_roi_extractor.num_inputs], teacher_proposal_rois[rand_index])
            vec = self.teacher.projector(teacher_proposal.view(teacher_proposal.size(0), -1))
            teacher_vec = torch.cat([teacher_vec, vec], dim=0)
        teacher_vec = F.normalize(teacher_vec, dim=1)
        
        assert student_vec.size(0) == teacher_vec.size(0) != 0 and student_vec.size(1) == teacher_vec.size(1) == self.projector_dim
        neg_logits = torch.einsum('nc,kc->nk', [student_vec, self.queue_vector.clone().detach()])
        pos_logits = torch.einsum('nc,nc->n', [student_vec, teacher_vec])
        logits = torch.cat([pos_logits[:, None], neg_logits], dim=1)
        logits /= self.ctr2_T
        labels = torch.zeros(logits.shape[0], dtype=torch.long).cuda()
        losses = dict()
        losses['ctr2_loss'] = F.cross_entropy(logits, labels)

        return losses

