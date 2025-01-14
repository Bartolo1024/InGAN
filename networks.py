import torch
import torch.nn as nn
import torch.nn.functional as f
from torch.autograd import Variable
import numpy as np
import non_rect
from util import homography_based_on_top_corners_x_shift, homography_grid


def weights_init(m):
    """ This is used to initialize weights of any network """
    class_name = m.__class__.__name__
    if class_name.find('Conv') != -1:
        nn.init.xavier_normal_(m.weight, 0.01)
        if hasattr(m.bias, 'data'):
            m.bias.data.fill_(0)
    elif class_name.find('nn.BatchNorm2d') != -1:
        m.weight.data.normal_(1.0, 0.02)
        m.bias.data.fill_(0)

    elif class_name.find('LocalNorm') != -1:
        m.weight.data.normal_(1.0, 0.02)
        m.bias.data.fill_(0)


class LocalNorm(nn.Module):
    def __init__(self, num_features):
        super(LocalNorm, self).__init__()
        self.weight = nn.Parameter(torch.Tensor(num_features))
        self.bias = nn.Parameter(torch.Tensor(num_features))
        self.get_local_mean = nn.AvgPool2d(33, 1, 16, count_include_pad=False)

        self.get_var = nn.AvgPool2d(33, 1, 16, count_include_pad=False)

    def forward(self, input_tensor):
        local_mean = self.get_local_mean(input_tensor)
        print(local_mean)
        centered_input_tensor = input_tensor - local_mean
        print(centered_input_tensor)
        squared_diff = centered_input_tensor**2
        print(squared_diff)
        local_std = self.get_var(squared_diff)**0.5
        print(local_std)
        normalized_tensor = centered_input_tensor / (local_std + 1e-8)

        return normalized_tensor  # * self.weight[None, :, None, None] + self.bias[None, :, None, None]


normalization_layer = nn.BatchNorm2d  # BatchReNorm2d  # LocalNorm


class GANLoss(nn.Module):
    """ Receiving the final layer form the discriminator and a boolean indicating whether the input to the
     discriminator is real or fake (generated by generator), this returns a patch"""
    def __init__(self):
        super(GANLoss, self).__init__()

        # Initialize label tensor
        self.label_tensor = None

        # Loss tensor is prepared in network initialization.
        # Note: When activated as a loss between to feature-maps, then a loss-map is created. However, using defaults
        # for BCEloss, this map is averaged and reduced to a single scalar
        self.loss = nn.MSELoss()

    def forward(self, d_last_layer, is_d_input_real):
        # Determine label map according to whether current input to discriminator is real or fake
        self.label_tensor = Variable(torch.ones_like(d_last_layer).cuda(), requires_grad=False) * is_d_input_real

        # Finally return the loss
        return self.loss(d_last_layer, self.label_tensor)


class WeightedMSELoss(nn.Module):
    def __init__(self, use_L1=False):
        super(WeightedMSELoss, self).__init__()

        self.unweighted_loss = nn.L1Loss() if use_L1 else nn.MSELoss()

    def forward(self, input_tensor, target_tensor, loss_mask):
        if loss_mask is not None:
            e = (target_tensor.detach() - input_tensor)**2
            e *= loss_mask
            return torch.sum(e) / torch.sum(loss_mask)
        else:
            return self.unweighted_loss(input_tensor, target_tensor)


class MultiScaleLoss(nn.Module):
    def __init__(self):
        super(MultiScaleLoss, self).__init__()

        self.mse = nn.MSELoss()

    def forward(self, input_tensor, target_tensor, scale_weights):

        # Run all nets over all scales and aggregate the interpolated results
        loss = 0
        for i, scale_weight in enumerate(scale_weights):
            input_tensor = f.interpolate(input_tensor, scale_factor=self.scale_factor**(-i), mode='bilinear')
            loss += scale_weight * self.mse(input_tensor, target_tensor)
        return loss


class Generator(nn.Module):
    """ Architecture of the Generator, uses res-blocks """
    def __init__(self, base_channels=64, n_blocks=6, n_downsampling=3, use_bias=True, skip_flag=True):
        super(Generator, self).__init__()

        # Determine whether to use skip connections
        self.skip = skip_flag

        # Entry block
        # First conv-block, no stride so image dims are kept and channels dim is expanded (pad-conv-norm-relu)
        self.entry_block = nn.Sequential(
            nn.ReflectionPad2d(3), nn.utils.spectral_norm(nn.Conv2d(3, base_channels, kernel_size=7, bias=use_bias)),
            normalization_layer(base_channels), nn.LeakyReLU(0.2, True)
        )

        # Geometric transformation
        self.geo_transform = GeoTransform()

        # Downscaling
        # A sequence of strided conv-blocks. Image dims shrink by 2, channels dim expands by 2 at each block
        self.downscale_block = RescaleBlock(n_downsampling, 0.5, base_channels, True)

        # Bottleneck
        # A sequence of res-blocks
        bottleneck_block = []
        for _ in range(n_blocks):
            # noinspection PyUnboundLocalVariable
            bottleneck_block += [ResnetBlock(base_channels * 2**n_downsampling, use_bias=use_bias)]
        self.bottleneck_block = nn.Sequential(*bottleneck_block)

        # Upscaling
        # A sequence of transposed-conv-blocks, Image dims expand by 2, channels dim shrinks by 2 at each block\
        self.upscale_block = RescaleBlock(n_downsampling, 2.0, base_channels, True)

        # Final block
        # No stride so image dims are kept and channels dim shrinks to 3 (output image channels)
        self.final_block = nn.Sequential(nn.ReflectionPad2d(3), nn.Conv2d(base_channels, 3, kernel_size=7), nn.Tanh())

    def forward(self, input_tensor, output_size, random_affine):
        # A condition for having the output at same size as the scaled input is having even output_size

        # Entry block
        feature_map = self.entry_block(input_tensor)

        # Change scale to output scale by interpolation
        if random_affine is None:
            feature_map = f.interpolate(feature_map, size=output_size, mode='bilinear')
        else:
            feature_map = self.geo_transform.forward(feature_map, output_size, random_affine)

        # Downscale block
        feature_map, downscales = self.downscale_block.forward(feature_map, return_all_scales=self.skip)

        # Bottleneck (res-blocks)
        feature_map = self.bottleneck_block(feature_map)

        # Upscale block
        feature_map, _ = self.upscale_block.forward(feature_map, pyramid=downscales, skip=self.skip)

        # Final block
        output_tensor = self.final_block(feature_map)

        return output_tensor


class ResnetBlock(nn.Module):
    """ A single Res-Block module """
    def __init__(self, dim, use_bias):
        super(ResnetBlock, self).__init__()

        # A res-block without the skip-connection, pad-conv-norm-relu-pad-conv-norm
        self.conv_block = nn.Sequential(
            nn.utils.spectral_norm(nn.Conv2d(dim, dim // 4, kernel_size=1, bias=use_bias)),
            normalization_layer(dim // 4), nn.LeakyReLU(0.2, True), nn.ReflectionPad2d(1),
            nn.utils.spectral_norm(nn.Conv2d(dim // 4, dim // 4, kernel_size=3, bias=use_bias)),
            normalization_layer(dim // 4), nn.LeakyReLU(0.2, True),
            nn.utils.spectral_norm(nn.Conv2d(dim // 4, dim, kernel_size=1, bias=use_bias)), normalization_layer(dim)
        )

    def forward(self, input_tensor):
        # The skip connection is applied here
        return input_tensor + self.conv_block(input_tensor)


class MultiScaleDiscriminator(nn.Module):
    def __init__(self, real_crop_size, max_n_scales=9, scale_factor=2, base_channels=128, extra_conv_layers=0):
        super(MultiScaleDiscriminator, self).__init__()
        self.base_channels = base_channels
        self.scale_factor = scale_factor
        self.min_size = 16
        self.extra_conv_layers = extra_conv_layers

        # We want the max num of scales to fit the size of the real examples. further scaling would create networks that
        # only train on fake examples
        self.max_n_scales = np.min(
            [
                np.int(np.ceil(np.log(np.min(real_crop_size) * 1.0 / self.min_size) / np.log(self.scale_factor))),
                max_n_scales
            ]
        )

        # Prepare a list of all the networks for all the wanted scales
        self.nets = nn.ModuleList()

        # Create a network for each scale
        for _ in range(self.max_n_scales):
            self.nets.append(self.make_net())

    def make_net(self):
        base_channels = self.base_channels
        net = []

        # Entry block
        net += [
            nn.utils.spectral_norm(nn.Conv2d(3, base_channels, kernel_size=3, stride=1)),
            nn.BatchNorm2d(base_channels),
            nn.LeakyReLU(0.2, True)
        ]

        # Downscaling blocks
        # A sequence of strided conv-blocks. Image dims shrink by 2, channels dim expands by 2 at each block
        net += [
            nn.utils.spectral_norm(nn.Conv2d(base_channels, base_channels * 2, kernel_size=3, stride=2)),
            nn.BatchNorm2d(base_channels * 2),
            nn.LeakyReLU(0.2, True)
        ]

        # Regular conv-block
        net += [
            nn.utils.spectral_norm(
                nn.Conv2d(in_channels=base_channels * 2, out_channels=base_channels * 2, kernel_size=3, bias=True)
            ),
            nn.BatchNorm2d(base_channels * 2),
            nn.LeakyReLU(0.2, True)
        ]

        # Additional 1x1 conv-blocks
        for _ in range(self.extra_conv_layers):
            net += [
                nn.utils.spectral_norm(
                    nn.Conv2d(in_channels=base_channels * 2, out_channels=base_channels * 2, kernel_size=3, bias=True)
                ),
                nn.BatchNorm2d(base_channels * 2),
                nn.LeakyReLU(0.2, True)
            ]

        # Final conv-block
        # Ends with a Sigmoid to get a range of 0-1
        net += nn.Sequential(nn.utils.spectral_norm(nn.Conv2d(base_channels * 2, 1, kernel_size=1)), nn.Sigmoid())

        # Make it a valid layers sequence and return
        return nn.Sequential(*net)

    def forward(self, input_tensor, scale_weights):
        aggregated_result_maps_from_all_scales = self.nets[0](input_tensor) * scale_weights[0]
        map_size = aggregated_result_maps_from_all_scales.shape[2:]

        # Run all nets over all scales and aggregate the interpolated results
        for net, scale_weight, i in zip(self.nets[1:], scale_weights[1:], list(range(1, len(scale_weights)))):
            downscaled_image = f.interpolate(input_tensor, scale_factor=self.scale_factor**(-i), mode='bilinear')
            result_map_for_current_scale = net(downscaled_image)
            upscaled_result_map_for_current_scale = f.interpolate(
                result_map_for_current_scale, size=map_size, mode='bilinear'
            )
            aggregated_result_maps_from_all_scales += upscaled_result_map_for_current_scale * scale_weight

        return aggregated_result_maps_from_all_scales


class RescaleBlock(nn.Module):
    def __init__(self, n_layers, scale=0.5, base_channels=64, use_bias=True):
        super(RescaleBlock, self).__init__()

        self.scale = scale

        self.conv_layers = [None] * n_layers

        in_channel_power = scale > 1
        out_channel_power = scale < 1
        i_range = list(range(n_layers)) if scale < 1 else list(range(n_layers - 1, -1, -1))

        for i in i_range:
            self.conv_layers[i] = nn.Sequential(
                nn.ReflectionPad2d(1),
                nn.utils.spectral_norm(
                    nn.Conv2d(
                        in_channels=base_channels * 2**(i + in_channel_power),
                        out_channels=base_channels * 2**(i + out_channel_power),
                        kernel_size=3,
                        stride=1,
                        bias=use_bias
                    )
                ), normalization_layer(base_channels * 2**(i + out_channel_power)), nn.LeakyReLU(0.2, True)
            )
            self.add_module("conv_%d" % i, self.conv_layers[i])

        if scale > 1:
            self.conv_layers = self.conv_layers[::-1]

        self.max_pool = nn.MaxPool2d(2, 2)

    def forward(self, input_tensor, pyramid=None, return_all_scales=False, skip=False):

        feature_map = input_tensor
        all_scales = []
        if return_all_scales:
            all_scales.append(feature_map)

        for i, conv_layer in enumerate(self.conv_layers):

            if self.scale > 1.0:
                feature_map = f.interpolate(feature_map, scale_factor=self.scale, mode='nearest')

            feature_map = conv_layer(feature_map)

            if skip:
                feature_map = feature_map + pyramid[-i - 2]

            if self.scale < 1.0:
                feature_map = self.max_pool(feature_map)

            if return_all_scales:
                all_scales.append(feature_map)

        return (feature_map, all_scales) if return_all_scales else (feature_map, None)


class RandomCrop(nn.Module):
    def __init__(self, crop_size, return_pos=False, must_divide=4.0):
        super(RandomCrop, self).__init__()

        # Determine crop size
        self.crop_size = crop_size
        self.must_divide = must_divide
        self.return_pos = return_pos

    def forward(self, input_tensors, crop_size=None):
        im_v_sz, im_h_sz = input_tensors[0].shape[2:]
        if crop_size is None:
            cr_v_sz, cr_h_sz = np.clip(self.crop_size, [0, 0], [im_v_sz - 1, im_h_sz - 1])
            cr_v_sz, cr_h_sz = np.uint32(
                np.floor(np.array([cr_v_sz, cr_h_sz]) * 1.0 / self.must_divide) * self.must_divide
            )
        else:
            cr_v_sz, cr_h_sz = crop_size

        top_left_v, top_left_h = [np.random.randint(0, im_v_sz - cr_v_sz), np.random.randint(0, im_h_sz - cr_h_sz)]

        out_tensors = [
            input_tensor[:, :, top_left_v:top_left_v + cr_v_sz,
                         top_left_h:top_left_h + cr_h_sz] if input_tensor is not None else None
            for input_tensor in input_tensors
        ]

        return (out_tensors, (top_left_v, top_left_h)) if self.return_pos else out_tensors


class SwapCrops(nn.Module):
    def __init__(self, min_crop_size, max_crop_size, mask_width=5):
        super(SwapCrops, self).__init__()

        self.rand_crop_1 = RandomCrop(None, return_pos=True)
        self.rand_crop_2 = RandomCrop(None, return_pos=True)

        self.min_crop_size = min_crop_size
        self.max_crop_size = max_crop_size

        self.mask_width = mask_width

    def forward(self, input_tensor):
        cr_v_sz, cr_h_sz = np.uint32(np.random.rand(2) * (self.max_crop_size - self.min_crop_size) + self.min_crop_size)

        [crop_1], (top_left_v_1, top_left_h_1) = self.rand_crop_1.forward([input_tensor], (cr_v_sz, cr_h_sz))
        [crop_2], (top_left_v_2, top_left_h_2) = self.rand_crop_1.forward([input_tensor], (cr_v_sz, cr_h_sz))

        output_tensor = torch.zeros_like(input_tensor)
        output_tensor[:, :, :, :] = input_tensor

        output_tensor[:, :, top_left_v_1:top_left_v_1 + cr_v_sz, top_left_h_1:top_left_h_1 + cr_h_sz] = crop_2
        output_tensor[:, :, top_left_v_2:top_left_v_2 + cr_v_sz, top_left_h_2:top_left_h_2 + cr_h_sz] = crop_1

        # Creating a mask. this is drawing a line in width 2*mask_width over the boundaries of the cropped image
        loss_mask = torch.ones_like(input_tensor)
        mw = self.mask_width
        loss_mask[:, :, top_left_v_1:top_left_v_1 + cr_v_sz, top_left_h_1 - mw:top_left_h_1 + mw] = 0
        loss_mask[:, :, top_left_v_1 - mw:top_left_v_1 + mw, top_left_h_1:top_left_h_1 + cr_h_sz] = 0
        loss_mask[:, :, top_left_v_1:top_left_v_1 + cr_v_sz,
                  top_left_h_1 + cr_h_sz - mw:top_left_h_1 + cr_h_sz + mw] = 0
        loss_mask[:, :, top_left_v_1 + cr_v_sz - mw:top_left_v_1 + cr_v_sz + mw,
                  top_left_h_1:top_left_h_1 + cr_h_sz] = 0
        loss_mask[:, :, top_left_v_2:top_left_v_2 + cr_v_sz, top_left_h_2 - mw:top_left_h_2 + mw] = 0
        loss_mask[:, :, top_left_v_2 - mw:top_left_v_2 + mw, top_left_h_2:top_left_h_2 + cr_h_sz] = 0
        loss_mask[:, :, top_left_v_2:top_left_v_2 + cr_v_sz,
                  top_left_h_2 + cr_h_sz - mw:top_left_h_2 + cr_h_sz + mw] = 0
        loss_mask[:, :, top_left_v_2 + cr_v_sz - mw:top_left_v_2 + cr_v_sz + mw,
                  top_left_h_2:top_left_h_2 + cr_h_sz] = 0

        return output_tensor, loss_mask


class GeoTransform(nn.Module):
    def __init__(self):
        super(GeoTransform, self).__init__()

    def forward(self, input_tensor, target_size, shifts):
        sz = input_tensor.shape
        theta = homography_based_on_top_corners_x_shift(shifts)

        pad = f.pad(
            input_tensor,
            (np.abs(np.int(np.ceil(sz[3] * shifts[0]))), np.abs(np.int(np.ceil(-sz[3] * shifts[1]))), 0, 0), 'reflect'
        )
        target_size4d = torch.Size([pad.shape[0], pad.shape[1], target_size[0], target_size[1]])

        grid = homography_grid(theta.expand(pad.shape[0], -1, -1), target_size4d)

        return f.grid_sample(pad, grid, mode='bilinear', padding_mode='border')
