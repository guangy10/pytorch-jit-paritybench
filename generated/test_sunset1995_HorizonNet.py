import sys
_module = sys.modules[__name__]
del sys
dataset = _module
eval_cuboid = _module
eval_general = _module
inference = _module
layout_viewer = _module
misc = _module
gen_txt_structured3d = _module
pano_lsd_align = _module
panostretch = _module
post_proc = _module
structured3d_extract_zip = _module
structured3d_prepare_dataset = _module
utils = _module
model = _module
preprocess = _module
train = _module

from _paritybench_helpers import _mock_config, patch_functional
from unittest.mock import mock_open, MagicMock
from torch.autograd import Function
from torch.nn import Module
import abc, collections, copy, enum, functools, inspect, itertools, logging, math, numbers, numpy, random, re, scipy, sklearn, string, tensorflow, time, torch, torchaudio, torchtext, torchvision, types, typing, uuid, warnings
import numpy as np
from torch import Tensor
patch_functional()
open = mock_open()
yaml = logging = sys = argparse = MagicMock()
ArgumentParser = argparse.ArgumentParser
_global_config = args = argv = cfg = config = params = _mock_config()
argparse.ArgumentParser.return_value.parse_args.return_value = _global_config
yaml.load.return_value = _global_config
sys.argv = _global_config
__version__ = '1.0.0'


import numpy as np


from scipy.spatial.distance import cdist


import torch


import torch.utils.data as data


from scipy.ndimage.filters import maximum_filter


import torch.nn as nn


import torch.nn.functional as F


from collections import OrderedDict


import torchvision.models as models


import functools


from torch import optim


from torch.utils.data import DataLoader


def lr_pad(x, padding=1):
    """ Pad left/right-most to each other instead of zero padding """
    return torch.cat([x[(...), -padding:], x, x[(...), :padding]], dim=3)


class LR_PAD(nn.Module):
    """ Pad left/right-most to each other instead of zero padding """

    def __init__(self, padding=1):
        super(LR_PAD, self).__init__()
        self.padding = padding

    def forward(self, x):
        return lr_pad(x, self.padding)


ENCODER_RESNET = ['resnet18', 'resnet34', 'resnet50', 'resnet101', 'resnet152', 'resnext50_32x4d', 'resnext101_32x8d']


class Resnet(nn.Module):

    def __init__(self, backbone='resnet50', pretrained=True):
        super(Resnet, self).__init__()
        assert backbone in ENCODER_RESNET
        self.encoder = getattr(models, backbone)(pretrained=pretrained)
        del self.encoder.fc, self.encoder.avgpool

    def forward(self, x):
        features = []
        x = self.encoder.conv1(x)
        x = self.encoder.bn1(x)
        x = self.encoder.relu(x)
        x = self.encoder.maxpool(x)
        x = self.encoder.layer1(x)
        features.append(x)
        x = self.encoder.layer2(x)
        features.append(x)
        x = self.encoder.layer3(x)
        features.append(x)
        x = self.encoder.layer4(x)
        features.append(x)
        return features

    def list_blocks(self):
        lst = [m for m in self.encoder.children()]
        block0 = lst[:4]
        block1 = lst[4:5]
        block2 = lst[5:6]
        block3 = lst[6:7]
        block4 = lst[7:8]
        return block0, block1, block2, block3, block4


ENCODER_DENSENET = ['densenet121', 'densenet169', 'densenet161', 'densenet201']


class Densenet(nn.Module):

    def __init__(self, backbone='densenet169', pretrained=True):
        super(Densenet, self).__init__()
        assert backbone in ENCODER_DENSENET
        self.encoder = getattr(models, backbone)(pretrained=pretrained)
        self.final_relu = nn.ReLU(inplace=True)
        del self.encoder.classifier

    def forward(self, x):
        lst = []
        for m in self.encoder.features.children():
            x = m(x)
            lst.append(x)
        features = [lst[4], lst[6], lst[8], self.final_relu(lst[11])]
        return features

    def list_blocks(self):
        lst = [m for m in self.encoder.features.children()]
        block0 = lst[:4]
        block1 = lst[4:6]
        block2 = lst[6:8]
        block3 = lst[8:10]
        block4 = lst[10:]
        return block0, block1, block2, block3, block4


class ConvCompressH(nn.Module):
    """ Reduce feature height by factor of two """

    def __init__(self, in_c, out_c, ks=3):
        super(ConvCompressH, self).__init__()
        assert ks % 2 == 1
        self.layers = nn.Sequential(nn.Conv2d(in_c, out_c, kernel_size=ks, stride=(2, 1), padding=ks // 2), nn.BatchNorm2d(out_c), nn.ReLU(inplace=True))

    def forward(self, x):
        return self.layers(x)


class GlobalHeightConv(nn.Module):

    def __init__(self, in_c, out_c):
        super(GlobalHeightConv, self).__init__()
        self.layer = nn.Sequential(ConvCompressH(in_c, in_c // 2), ConvCompressH(in_c // 2, in_c // 2), ConvCompressH(in_c // 2, in_c // 4), ConvCompressH(in_c // 4, out_c))

    def forward(self, x, out_w):
        x = self.layer(x)
        assert out_w % x.shape[3] == 0
        factor = out_w // x.shape[3]
        x = torch.cat([x[(...), -1:], x, x[(...), :1]], 3)
        x = F.interpolate(x, size=(x.shape[2], out_w + 2 * factor), mode='bilinear', align_corners=False)
        x = x[(...), factor:-factor]
        return x


class GlobalHeightStage(nn.Module):

    def __init__(self, c1, c2, c3, c4, out_scale=8):
        """ Process 4 blocks from encoder to single multiscale features """
        super(GlobalHeightStage, self).__init__()
        self.cs = c1, c2, c3, c4
        self.out_scale = out_scale
        self.ghc_lst = nn.ModuleList([GlobalHeightConv(c1, c1 // out_scale), GlobalHeightConv(c2, c2 // out_scale), GlobalHeightConv(c3, c3 // out_scale), GlobalHeightConv(c4, c4 // out_scale)])

    def forward(self, conv_list, out_w):
        assert len(conv_list) == 4
        bs = conv_list[0].shape[0]
        feature = torch.cat([f(x, out_w).reshape(bs, -1, out_w) for f, x, out_c in zip(self.ghc_lst, conv_list, self.cs)], dim=1)
        return feature


def wrap_lr_pad(net):
    for name, m in net.named_modules():
        if not isinstance(m, nn.Conv2d):
            continue
        if m.padding[1] == 0:
            continue
        w_pad = int(m.padding[1])
        m.padding = m.padding[0], 0
        names = name.split('.')
        root = functools.reduce(lambda o, i: getattr(o, i), [net] + names[:-1])
        setattr(root, names[-1], nn.Sequential(LR_PAD(w_pad), m))


class HorizonNet(nn.Module):
    x_mean = torch.FloatTensor(np.array([0.485, 0.456, 0.406])[(None), :, (None), (None)])
    x_std = torch.FloatTensor(np.array([0.229, 0.224, 0.225])[(None), :, (None), (None)])

    def __init__(self, backbone, use_rnn):
        super(HorizonNet, self).__init__()
        self.backbone = backbone
        self.use_rnn = use_rnn
        self.out_scale = 8
        self.step_cols = 4
        self.rnn_hidden_size = 512
        if backbone.startswith('res'):
            self.feature_extractor = Resnet(backbone, pretrained=True)
        elif backbone.startswith('dense'):
            self.feature_extractor = Densenet(backbone, pretrained=True)
        else:
            raise NotImplementedError()
        with torch.no_grad():
            dummy = torch.zeros(1, 3, 512, 1024)
            c1, c2, c3, c4 = [b.shape[1] for b in self.feature_extractor(dummy)]
            c_last = (c1 * 8 + c2 * 4 + c3 * 2 + c4 * 1) // self.out_scale
        self.reduce_height_module = GlobalHeightStage(c1, c2, c3, c4, self.out_scale)
        if self.use_rnn:
            self.bi_rnn = nn.LSTM(input_size=c_last, hidden_size=self.rnn_hidden_size, num_layers=2, dropout=0.5, batch_first=False, bidirectional=True)
            self.drop_out = nn.Dropout(0.5)
            self.linear = nn.Linear(in_features=2 * self.rnn_hidden_size, out_features=3 * self.step_cols)
            self.linear.bias.data[0 * self.step_cols:1 * self.step_cols].fill_(-1)
            self.linear.bias.data[1 * self.step_cols:2 * self.step_cols].fill_(-0.478)
            self.linear.bias.data[2 * self.step_cols:3 * self.step_cols].fill_(0.425)
        else:
            self.linear = nn.Sequential(nn.Linear(c_last, self.rnn_hidden_size), nn.ReLU(inplace=True), nn.Dropout(0.5), nn.Linear(self.rnn_hidden_size, 3 * self.step_cols))
            self.linear[-1].bias.data[0 * self.step_cols:1 * self.step_cols].fill_(-1)
            self.linear[-1].bias.data[1 * self.step_cols:2 * self.step_cols].fill_(-0.478)
            self.linear[-1].bias.data[2 * self.step_cols:3 * self.step_cols].fill_(0.425)
        self.x_mean.requires_grad = False
        self.x_std.requires_grad = False
        wrap_lr_pad(self)

    def _prepare_x(self, x):
        if self.x_mean.device != x.device:
            self.x_mean = self.x_mean
            self.x_std = self.x_std
        return (x[:, :3] - self.x_mean) / self.x_std

    def forward(self, x):
        if x.shape[2] != 512 or x.shape[3] != 1024:
            raise NotImplementedError()
        x = self._prepare_x(x)
        conv_list = self.feature_extractor(x)
        feature = self.reduce_height_module(conv_list, x.shape[3] // self.step_cols)
        if self.use_rnn:
            feature = feature.permute(2, 0, 1)
            output, hidden = self.bi_rnn(feature)
            output = self.drop_out(output)
            output = self.linear(output)
            output = output.view(output.shape[0], output.shape[1], 3, self.step_cols)
            output = output.permute(1, 2, 0, 3)
            output = output.contiguous().view(output.shape[0], 3, -1)
        else:
            feature = feature.permute(0, 2, 1)
            output = self.linear(feature)
            output = output.view(output.shape[0], output.shape[1], 3, self.step_cols)
            output = output.permute(0, 2, 1, 3)
            output = output.contiguous().view(output.shape[0], 3, -1)
        cor = output[:, :1]
        bon = output[:, 1:]
        return bon, cor


import torch
from torch.nn import MSELoss, ReLU
from _paritybench_helpers import _mock_config, _mock_layer, _paritybench_base, _fails_compile


TESTCASES = [
    # (nn.Module, init_args, forward_args, jit_compiles)
    (ConvCompressH,
     lambda: ([], {'in_c': 4, 'out_c': 4}),
     lambda: ([torch.rand([4, 4, 4, 4])], {}),
     True),
    (Densenet,
     lambda: ([], {}),
     lambda: ([torch.rand([4, 3, 64, 64])], {}),
     False),
    (LR_PAD,
     lambda: ([], {}),
     lambda: ([torch.rand([4, 4, 4, 4])], {}),
     False),
    (Resnet,
     lambda: ([], {}),
     lambda: ([torch.rand([4, 3, 64, 64])], {}),
     False),
]

class Test_sunset1995_HorizonNet(_paritybench_base):
    def test_000(self):
        self._check(*TESTCASES[0])

    def test_001(self):
        self._check(*TESTCASES[1])

    def test_002(self):
        self._check(*TESTCASES[2])

    def test_003(self):
        self._check(*TESTCASES[3])

