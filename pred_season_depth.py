from __future__ import absolute_import, division, print_function

import os
import cv2
import numpy as np

import torch
from torch.utils.data import DataLoader

from layers import disp_to_depth
from utils import readlines
from options import MonodepthOptions
import datasets
import networks
import tqdm

cv2.setNumThreads(0)  # This speeds up evaluation 5x on our unix systems (OpenCV 3.3.1)


splits_dir = os.path.join(os.path.dirname(__file__), "splits")

# Models which were trained with stereo supervision were trained with a nominal
# baseline of 0.1 units. The KITTI rig has a baseline of 54cm. Therefore,
# to convert our stereo predictions to real-world scale we multiply our depths by 5.4.
STEREO_SCALE_FACTOR = 1.0


def compute_errors(gt, pred):
    """Computation of error metrics between predicted and ground truth depths
    """
    thresh = np.maximum((gt / pred), (pred / gt))
    a1 = (thresh < 1.25     ).mean()
    a2 = (thresh < 1.25 ** 2).mean()
    a3 = (thresh < 1.25 ** 3).mean()

    rmse = (gt - pred) ** 2
    rmse = np.sqrt(rmse.mean())

    rmse_log = (np.log(gt) - np.log(pred)) ** 2
    rmse_log = np.sqrt(rmse_log.mean())

    abs_rel = np.mean(np.abs(gt - pred) / gt)

    sq_rel = np.mean(((gt - pred) ** 2) / gt)

    return abs_rel, sq_rel, rmse, rmse_log, a1, a2, a3


def batch_post_process_disparity(l_disp, r_disp):
    """Apply the disparity post-processing method as introduced in Monodepthv1
    """
    _, h, w = l_disp.shape
    m_disp = 0.5 * (l_disp + r_disp)
    l, _ = np.meshgrid(np.linspace(0, 1, w), np.linspace(0, 1, h))
    l_mask = (1.0 - np.clip(20 * (l - 0.05), 0, 1))[None, ...]
    r_mask = l_mask[:, :, ::-1]
    return r_mask * l_disp + l_mask * r_disp + (1.0 - l_mask - r_mask) * m_disp


def evaluate(opt):
    """Evaluates a pretrained model using a specified test set
    """
    MIN_DEPTH = 1e-3
    MAX_DEPTH = 80

    assert sum((opt.eval_mono, opt.eval_stereo)) == 1, \
        "Please choose mono or stereo evaluation by setting either --eval_mono or --eval_stereo"
    if opt.eval_mono:
        STEREO_SCALE_FACTOR = 1.0
    elif opt.eval_stereo:
        STEREO_SCALE_FACTOR = 5.4

    if opt.ext_disp_to_eval is None:

        opt.load_weights_folder = os.path.expanduser(opt.load_weights_folder)

        assert os.path.isdir(opt.load_weights_folder), \
            "Cannot find a folder at {}".format(opt.load_weights_folder)

        print("-> Loading weights from {}".format(opt.load_weights_folder))

        filenames = readlines(os.path.join(splits_dir, opt.eval_split, opt.eval_set+"_files.txt"))
        encoder_path = os.path.join(opt.load_weights_folder, "encoder.pth")
        decoder_path = os.path.join(opt.load_weights_folder, "depth.pth")

        encoder_dict = torch.load(encoder_path)

        dataset = datasets.SeasonTestDataset(opt.data_path, filenames,
                                           encoder_dict['height'], encoder_dict['width'],
                                           [0], [0], is_train=False)
        dataloader = DataLoader(dataset, opt.batch_size, shuffle=False, num_workers=opt.num_workers,
                                pin_memory=True, drop_last=False)

        if opt.encoder=="resnet":
            encoder = networks.ResnetEncoder(opt.num_layers, False)
            depth_decoder = networks.DepthDecoder(encoder.num_ch_enc)
        elif opt.encoder=="van":
            encoder = networks.VANEncoder(opt.size_encoder, False)
            depth_decoder = networks.VANDecoder(encoder.num_ch_enc, opt.scales, opt.height, opt.width)

        model_dict = encoder.state_dict()
        encoder.load_state_dict({k: v for k, v in encoder_dict.items() if k in model_dict})
        depth_decoder.load_state_dict(torch.load(decoder_path))

        encoder.cuda()
        encoder.eval()
        depth_decoder.cuda()
        depth_decoder.eval()

        pred_disps = []

        print("-> Computing predictions with size {}x{}".format(
            encoder_dict['width'], encoder_dict['height']))

        save_dir = opt.pred_depth_path
        print("-> Saving out season depth predictions to {}".format(save_dir))
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)
        
        print("-> Generating {} depth images".format(len(filenames)))
        with torch.no_grad():
            j = 0
            for data in tqdm.tqdm(dataloader):
                pred_disps = []
                input_color = data[("color", 0, 0)].cuda()

                if opt.post_process:
                    # Post-processed results require each image to have two forward passes
                    input_color = torch.cat((input_color, torch.flip(input_color, [3])), 0)

                output = depth_decoder(encoder(input_color))

                pred_disp, _ = disp_to_depth(output[("disp", 0)], opt.min_depth, opt.max_depth)
                pred_disp = pred_disp.cpu()[:, 0].numpy()

                if opt.post_process:
                    N = pred_disp.shape[0] // 2
                    pred_disp = batch_post_process_disparity(pred_disp[:N], pred_disp[N:, :, ::-1])

                pred_disps.append(pred_disp)

                pred_disps = np.concatenate(pred_disps)

                for idx in range(len(pred_disps)):
                    disp_resized = cv2.resize(pred_disps[idx], (1024, 768))
                    #disp_resized = cv2.resize(pred_disps[idx], (640, 192))
                    depth = STEREO_SCALE_FACTOR / disp_resized
                    depth = np.clip(depth, 0, 80)
                    depth = np.uint16(depth * 256)
                    fdir, fn = filenames[j].split()
                    j += 1
                    #save_path = os.path.join(save_dir, fdir.split("/")[0])
                    #save_path = os.path.join(save_dir, fdir)
                    #if not os.path.exists(save_path):
                    #    os.makedirs(save_path)
                    save_path = os.path.join(save_dir,  "img_"+fn+"us.png")
                    #print(j,save_path)
                    cv2.imwrite(save_path, depth)

        print("-> Done.")
        quit()


if __name__ == "__main__":
    options = MonodepthOptions()
    evaluate(options.parse())