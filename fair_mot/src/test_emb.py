from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import _init_paths
import argparse
import torch
import json
import time
import os
import cv2
import math

from sklearn import metrics
from scipy import interpolate
import numpy as np
from torchvision.transforms import transforms as T
import torch.nn.functional as F
from lib.models.model import create_model, load_model
from lib.datasets.dataset.jde import JointDataset, collate_fn
from lib.models.utils import _tranpose_and_gather_feat
from lib.utils.utils import xywh2xyxy, ap_per_class, bbox_iou
from lib.opts import opts
from lib.models.decode import mot_decode
from lib.utils.post_process import ctdet_post_process


def test_emb(
        opt,
        batch_size=16,
        img_size=(1088, 608),
        print_interval=40,
):
    data_cfg = opt.data_cfg
    f = open(data_cfg)
    data_cfg_dict = json.load(f)
    f.close()
    nC = 1
    test_paths = data_cfg_dict['test_emb']
    dataset_root = data_cfg_dict['root']
    if opt.gpus[0] >= 0:
        opt.device = torch.device('cuda')
    else:
        opt.device = torch.device('cpu')
    print('Creating model...')
    model = create_model(opt.arch, opt.heads, opt.head_conv)
    model = load_model(model, opt.load_model)
    # model = torch.nn.DataParallel(model)
    model = model.to(opt.device)
    model.eval()

    # Get dataloader
    transforms = T.Compose([T.ToTensor()])
    dataset = JointDataset(opt, dataset_root, test_paths, img_size, augment=False, transforms=transforms)
    dataloader = torch.utils.data.DataLoader(dataset, batch_size=batch_size, shuffle=False,
                                             num_workers=8, drop_last=False)
    embedding, id_labels = [], []
    print('Extracting pedestrain features...')
    for batch_i, batch in enumerate(dataloader):
        t = time.time()
        output = model(batch['input'].cuda())[-1]
        id_head = _tranpose_and_gather_feat(output['id'], batch['ind'].cuda())
        id_head = id_head[batch['reg_mask'].cuda() > 0].contiguous()
        # attr_head = _tranpose_and_gather_feat(output['attr'], batch['ind'].cuda())
        # attr_head = attr_head[batch['reg_mask'] > 0].contiguous()

        emb_scale = math.sqrt(2) * math.log(opt.nID - 1)
        id_head = emb_scale * F.normalize(id_head)
        # m = torch.nn.Sigmoid()
        # attr_head = m(attr_head)
        # id_head = torch.cat((id_head, attr_head), dim=1)

        id_target = batch['ids'].cuda()[batch['reg_mask'].cuda() > 0]

        for i in range(0, id_head.shape[0]):
            if len(id_head.shape) == 0:
                continue
            else:
                feat, label = id_head[i], id_target[i].long()
            if label != -1:
                embedding.append(feat)
                id_labels.append(label)

        if batch_i % print_interval == 0:
            print(
                'Extracting {}/{}, # of instances {}, time {:.2f} sec.'.format(batch_i, len(dataloader), len(id_labels),
                                                                               time.time() - t))

    print('Computing pairwise similairity...')
    if len(embedding) < 1:
        return None
    embedding = torch.stack(embedding, dim=0).cuda()
    id_labels = torch.LongTensor(id_labels)
    n = len(id_labels)
    print(n, len(embedding))
    assert len(embedding) == n

    embedding = F.normalize(embedding, dim=1)
    pdist = torch.mm(embedding, embedding.t()).cpu().numpy()
    gt = id_labels.expand(n, n).eq(id_labels.expand(n, n).t()).numpy()

    up_triangle = np.where(np.triu(pdist) - np.eye(n) * pdist != 0)
    pdist = pdist[up_triangle]
    gt = gt[up_triangle]

    far_levels = [1e-6, 1e-5, 1e-4, 1e-3, 1e-2, 1e-1]
    far, tar, threshold = metrics.roc_curve(gt, pdist)
    interp = interpolate.interp1d(far, tar)
    tar_at_far = [interp(x) for x in far_levels]
    for f, fa in enumerate(far_levels):
        print('TPR@FAR={:.7f}: {:.4f}'.format(fa, tar_at_far[f]))
    return tar_at_far

if __name__ == '__main__':
    os.environ['CUDA_VISIBLE_DEVICES'] = '1'
    opt = opts().init()
    with torch.no_grad():
        tpr = test_emb(opt, batch_size=4)
