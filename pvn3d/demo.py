#!/usr/bin/env python3
from __future__ import (
    division,
    absolute_import,
    with_statement,
    print_function,
    unicode_literals,
)
import os
import datetime
import tqdm
import cv2
import torch
import argparse
import torch.nn as nn
import numpy as np
import pickle as pkl
from common import Config
from lib import PVN3D
from datasets.ycb.ycb_dataset import YCB_Dataset
from datasets.linemod.linemod_dataset import LM_Dataset
from lib.utils.sync_batchnorm import convert_model
from lib.utils.pvn3d_eval_utils import cal_frame_poses, cal_frame_poses_lm
from lib.utils.basic_utils import Basic_Utils
try:
    from neupeak.utils.webcv2 import imshow, waitKey
except:
    from cv2 import imshow, waitKey


parser = argparse.ArgumentParser(description="Arg parser")
parser.add_argument(
    "-checkpoint", type=str, default='/home/ubuntu/workplace/PVN3D/pvn3d/train_log/ycb/checkpoints/pvn3d_best', help="Checkpoint to eval"
)
parser.add_argument(
    "-dataset", type=str, default="ycb",
    help="Target dataset, ycb or linemod. (linemod as default)."
)
parser.add_argument(
    "-cls", type=str, default="ape",
    help="Target object to eval in LineMOD dataset. (ape, benchvise, cam, can," +
    "cat, driller, duck, eggbox, glue, holepuncher, iron, lamp, phone)"
)
args = parser.parse_args()

if args.dataset == "ycb":
    config = Config(dataset_name=args.dataset)
else:
    config = Config(dataset_name=args.dataset, cls_type=args.cls)
bs_utils = Basic_Utils(config)


def ensure_fd(fd):
    if not os.path.exists(fd):
        os.system('mkdir -p {}'.format(fd))


def checkpoint_state(model=None, optimizer=None, best_prec=None, epoch=None, it=None):
    optim_state = optimizer.state_dict() if optimizer is not None else None
    if model is not None:
        if isinstance(model, torch.nn.DataParallel):
            model_state = model.module.state_dict()
        else:
            model_state = model.state_dict()
    else:
        model_state = None
    return {
        "epoch": epoch,
        "it": it,
        "best_prec": best_prec,
        "model_state": model_state,
        "optimizer_state": optim_state,
    }


def load_checkpoint(model=None, optimizer=None, filename="checkpoint"):
    filename = "{}.pth.tar".format(filename)

    if os.path.isfile(filename):
        print("==> Loading from checkpoint '{}'".format(filename))
        try:
            checkpoint = torch.load(filename)
        except:
            checkpoint = pkl.load(open(filename, "rb"))
        epoch = checkpoint["epoch"]
        it = checkpoint.get("it", 0.0)
        best_prec = checkpoint["best_prec"]
        if model is not None and checkpoint["model_state"] is not None:
            model.load_state_dict(checkpoint["model_state"])
        if optimizer is not None and checkpoint["optimizer_state"] is not None:
            optimizer.load_state_dict(checkpoint["optimizer_state"])
        print("==> Done")
        return it, epoch, best_prec
    else:
        print("==> Checkpoint '{}' not found".format(filename))
        return None


def cal_view_pred_pose(model, data, epoch=0, obj_id=-1):
    model.eval()
    with torch.set_grad_enabled(False):
        cu_dt = [item.to("cuda", non_blocking=True) for item in data]
        rgb, pcld, cld_rgb_nrm, choose, kp_targ_ofst, ctr_targ_ofst, \
            cls_ids, rts, labels, kp_3ds, ctr_3ds = cu_dt

        pred_kp_of, pred_rgbd_seg, pred_ctr_of = model(
            cld_rgb_nrm, rgb, choose
        )
        _, classes_rgbd = torch.max(pred_rgbd_seg, -1)

        if args.dataset == "ycb":
            pred_cls_ids, pred_pose_lst = cal_frame_poses(
                pcld[0], classes_rgbd[0], pred_ctr_of[0], pred_kp_of[0], True,
                config.n_objects, True
            )
        else:
            pred_pose_lst = cal_frame_poses_lm(
                pcld[0], classes_rgbd[0], pred_ctr_of[0], pred_kp_of[0], True,
                config.n_objects, False, obj_id
            )
            pred_cls_ids = np.array([[1]])

        # print("pred_pose_lst" + str(pred_pose_lst))

        np_rgb = rgb.cpu().numpy().astype("uint8")[0].transpose(1, 2, 0).copy()
        if args.dataset == "ycb":
            np_rgb = np_rgb[:, :, ::-1].copy()
        ori_rgb = np_rgb.copy()

        object_pose_dict = {}
        cls_lst = bs_utils.read_lines(config.ycb_cls_lst_p)
        object_count = 0
        for cls_id in cls_ids[0].cpu().numpy():
            idx = np.where(pred_cls_ids == cls_id)[0]
            if len(idx) == 0:
                continue
            for cls_idx in idx:
                pose = pred_pose_lst[cls_idx]
                if args.dataset == "ycb":
                    obj_id = int(cls_id[0])
                    obj_name = cls_lst[obj_id-1][4:]
                    print(obj_name)
                    print("pose:" + str(pose))
                    if obj_name in object_pose_dict.keys():
                        object_pose_dict[obj_name].append(pose)
                    else:
                        object_pose_dict[obj_name] = [pose]
                mesh_pts = bs_utils.get_pointxyz(obj_id, ds_type=args.dataset).copy()
                mesh_pts = np.dot(mesh_pts, pose[:, :3].T) + pose[:, 3]
                if args.dataset == "ycb":
                    K = config.intrinsic_matrix["ycb_K1"]
                else:
                    K = config.intrinsic_matrix["linemod"]
                mesh_p2ds = bs_utils.project_p3d(mesh_pts, 1.0, K)
                color = bs_utils.get_label_color(obj_id, n_obj=22, mode=1)
                np_rgb = bs_utils.draw_p2ds(np_rgb, mesh_p2ds, color=color)
                cv2.putText(np_rgb, obj_name + '_' + str(len(object_pose_dict[obj_name])), (np.min(mesh_p2ds[:, 0]), np.min(mesh_p2ds[:, 1])), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
                object_count += 1
        vis_dir = os.path.join(config.log_eval_dir, "pose_vis")
        ensure_fd(vis_dir)
        # append time, to keep the historial images
        curr_time = datetime.datetime.now()
        append_str = str(curr_time.month) + str(curr_time.day) + str(curr_time.hour) + str(curr_time.minute) + str(curr_time.second)
        f_pth = os.path.join(vis_dir,  "0.jpg")
        f_pth_with_time = os.path.join(vis_dir,  "{}".format(epoch) + append_str + ".jpg")
        org_f_pth = os.path.join(vis_dir,  "org_{}".format(epoch) + append_str + ".jpg")
        cv2.imwrite(f_pth, np_rgb)
        cv2.imwrite(f_pth_with_time, np_rgb)
        cv2.imwrite(org_f_pth, ori_rgb)
        # save the pose results
        pose_pth = os.path.join(vis_dir, "{}_pose_dict.npy".format(epoch))
        print(object_pose_dict)
        np.save(pose_pth, object_pose_dict)
        print("\n\nPose results saved in: {}".format(pose_pth))
        # pose_2 = np.loadtxt(pose_pth) 
        # print(pose_2)
        imshow("projected_pose_rgb", np_rgb)
        # imshow("ori_rgb", ori_rgb)
        waitKey(1)
    if epoch == 0:
        print("\n\nResults saved in {}".format(vis_dir))


def main():
    if args.dataset == "ycb":
        test_ds = YCB_Dataset('test')
        obj_id = -1
    else:
        test_ds = LM_Dataset('test', cls_type=args.cls)
        obj_id = config.lm_obj_dict[args.cls]
        
    test_loader = torch.utils.data.DataLoader(
        test_ds, batch_size=config.test_mini_batch_size, shuffle=False,
        num_workers=0
    )
    torch.cuda.empty_cache()
    with torch.no_grad():
        model = PVN3D(
            num_classes=config.n_objects, pcld_input_channels=6, pcld_use_xyz=True,
            num_points=config.n_sample_points
        ).cuda()
        model = convert_model(model)
        model.cuda()

        # load status from checkpoint
        if args.checkpoint is not None:
            checkpoint_status = load_checkpoint(
                model, None, filename=args.checkpoint
            )
        model = nn.DataParallel(model)

        for i, data in tqdm.tqdm(
            enumerate(test_loader), leave=False, desc="val"
        ):
            cal_view_pred_pose(model, data, epoch=i, obj_id=obj_id)


if __name__ == "__main__":
    main()

# vim: ts=4 sw=4 sts=4 expandtab
