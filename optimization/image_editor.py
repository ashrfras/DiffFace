import os
import cv2
import glob
import lpips
import numpy as np
import face_alignment

from PIL import Image
from numpy import random
from pathlib import Path

from utils.metrics_accumulator import MetricsAccumulator

from optimization.constants import ASSETS_DIR_NAME, RANKED_RESULTS_DIR
from optimization.augmentations import ImageAugmentations, StructureAugmentations

import torch
import torch.nn.functional as F
from torchvision import transforms
from torch.utils.data import DataLoader
from torchvision.transforms import Resize
from torch.nn.functional import mse_loss, l1_loss
from torchvision.utils import save_image, make_grid
from torchvision.transforms import functional as TF

from utils.visualization import show_tensor_image, show_editied_masked_image
from utils.module import SpecificNorm, cosin_metric
from models.parsing import BiSeNet

from models.guided_diffusion.script_util import (
    create_model_and_diffusion,
    model_and_diffusion_defaults,
)

# Gaze
from utils.eye_crop import get_eye_coords
from models.gaze_estimation.gaze_estimator import Gaze_estimator

# Load and resize images from given path
class VGGDataset(torch.utils.data.Dataset):
    def __init__(self, path, img_size=256):
        super().__init__()
        self.files = glob.glob(path + '/**/*.jpg', recursive=True)
        self.files.extend(glob.glob(path + '/**/*.png', recursive=True))
        self.files.sort()
        self.img_size = img_size
        self.transform = transforms.Compose([transforms.Resize((img_size, img_size)), transforms.ToTensor(),])

    def __getitem__(self, index):
        file = self.files[index]
        image = Image.open(file).convert('RGB')
        x = self.transform(image)
        return x

    def __len__(self):
        return len(self.files)

# Main class
class ImageEditor:
    def __init__(self, args) -> None:
        self.args = args
        
        # Recursivly create directories in output_path param, files will be overwritten if already exist
        os.makedirs(self.args.output_path, exist_ok=True)
        # We store the ranked samples in "/output/ranked"
        self.ranked_results_path = Path(os.path.join(self.args.output_path, RANKED_RESULTS_DIR))

        # default seed in arguments is 404
        if self.args.seed is not None:
            torch.manual_seed(self.args.seed) # Sets the seed for generating random numbers
            np.random.seed(self.args.seed) # Reseed the singleton RandomState instance
            random.seed(self.args.seed) # Seeding the pseudo-random number generator

        # Defaults for guided diffusion image training (models.guided_diffusion.script_util)
        self.model_config = model_and_diffusion_defaults()
        # Now overwriting some defaults
        self.model_config.update(
            {
                # a collection of downsample rates at whichattention will take place.
                # May be a set, list, or tuple.
                # For example, if this contains 4, then at 4x downsampling, attention will be used.
                "attention_resolutions": "32, 16, 8",
                # if True, include a "y" key in returned dicts for class label.
                # If classes are not available and this is true, an exception will be raised.
                "class_cond": False,
                "diffusion_steps": 1000,
                # if True, pass floating point timesteps into the model so that they are always scaled like in the
                # original paper (0 to 1000)
                "rescale_timesteps": True,
                "timestep_respacing": self.args.timestep_respacing, # density of sampling, default=100
                "image_size": 256, # our model was trained on FFHQ dataset with 256 x 256 aligned face images
                "learn_sigma": True,
                "noise_schedule": "linear",
                "num_channels": 256,
                "num_head_channels": 64,
                "num_res_blocks": 2,
                "resblock_updown": True,
                "use_fp16": True, # use FP16 precision for faster computation
                "use_scale_shift_norm": True,
            }
        )

        # Load models
        # Load cuda default 0 or use CPU if no GPU
        self.device = torch.device(f"cuda:{self.args.gpu_id}" if torch.cuda.is_available() else "cpu")
        print("Using device:", self.device)

        # Creating model and diffusion (models.guided_diffusion.script_util)
        self.model, self.diffusion = create_model_and_diffusion(**self.model_config)
        
        # Loads model’s parameter dictionary
        # checkpoints/Model.pt is our ID Conditional DDPM trained model with 700k steps (on FFHQ dataset)
        self.model.load_state_dict(
            torch.load(
                "checkpoints/Model.pt",
                map_location="cpu",
            )
        )
        
        # Do not record operations on tensor
        # Set the module in evaluation mode on selected device
        self.model.requires_grad_(False).eval().to(self.device)

        
        for name, param in self.model.named_parameters():
            if "qkv" in name or "norm" in name or "proj" in name:
                param.requires_grad_()

        # Use FP16 precision for faster computation
        # Converts all floating point tensors in the model to 16-bit precision
        if self.model_config["use_fp16"]:
            self.model.convert_to_fp16()

        # Learned Perceptual Image Patch Similarity (LPIPS)
        # This metric evaluate the distance between image patches. Higher means further/more different. Lower means more similar.
        self.lpips_model = lpips.LPIPS(net="vgg").to(self.device)

        # Load augmentations (optimization/augmentation)
        self.image_structureAugmentations = StructureAugmentations(224, self.args.aug_num // 2)
        self.image_augmentations = ImageAugmentations(112, self.args.aug_num)
        self.metrics_accumulator = MetricsAccumulator()

        # Load and evaluate the ArcFace face recognition model 
        netArc_checkpoint = torch.load('./checkpoints/Arcface.tar')
        netArc = netArc_checkpoint['model'].module
        self.netArc = netArc.to(self.device).eval()

        # Load and evaluate the FaceParser, the face parser model
        self.spNorm = SpecificNorm()
        self.netSeg = BiSeNet(n_classes=19).to(self.device)
        self.netSeg.load_state_dict(torch.load('./checkpoints/FaceParser.pth'))
        self.netSeg.eval()

        # Load the gaze estimator model
        self.netGaze = Gaze_estimator().to(self.device)

        # Detect facial landmarks
        self.fa = face_alignment.FaceAlignment(face_alignment.LandmarksType.TWO_D, flip_input=False)

        print('done')
        

    def unscale_timestep(self, t):
        unscaled_timestep = (t * (self.diffusion.num_timesteps / 1000)).long()
        return unscaled_timestep

    # takes two images src and targ, processes them through netarc (face recognition) to obtain their identity representations,
    # and computes the identity loss by measuring the cosine similarity between these representations
    def id_distance(self, src, targ):
        # Convert the src image to a PyTorch tensor
        # and Add an extra dimension to the tensor using unsqueeze(0) to represent the batch dimension.
        src = TF.to_tensor(src).unsqueeze(0).to(self.device) 
        # Normalize the tensor using the mean and standard deviation values provided in the list
        src = transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])(src)
        # Resize the tensor to a size of (112, 112)
        src = F.interpolate(src, (112, 112))
        # Passe the normalized and resized tensor through netarc (face recognition) to obtain the identity representation src_id
        src_id = self.netArc(src)
        
        # Perform the same operations above on the targ image to obtain the identity representation targ_id
        targ = TF.to_tensor(targ).unsqueeze(0).to(self.device)
        targ = transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])(targ)
        targ = F.interpolate(targ, (112, 112))
        targ_id = self.netArc(targ)
        
        # Calculate the cosine similarity metric between src_id and targ_id using the cosin_metric function.
        # The 1 - cosin_metric is used as the identity loss
        id_loss = 1 - cosin_metric(src_id, targ_id)

        # orints the value of the identity loss
        print('ID loss: {}'.format(id_loss.item()))
        return id_loss.item()


    # this function takes an input mask, selects specific classes from the mask based on their IDs (skin, nose, mouth...), 
    # and creates a binary mask where the selected classes are represented by 1 and the rest are represented by 0. 
    # The resulting modified mask is then returned
    def makeMask(self, origin_mask):
        numpy = origin_mask.squeeze(0).detach().cpu().numpy().argmax(0)
        numpy = numpy.copy().astype(np.uint8)

        # atts = [1 'skin', 2 'l_brow', 3 'r_brow', 4 'l_eye', 5 'r_eye', 6 'eye_g', 7 'l_ear', 8 'r_ear', 9 'ear_r', 10 'nose', 11 'mouth', 12 'u_lip', 13 'l_lip', 14 'neck', 15 'neck_l', 16 'cloth', 17 'hair', 18 'hat']
        ids = [1, 2, 3, 4, 5, 10, 11, 12, 13]

        mask     = np.zeros([256, 256])
        for id in ids:
            index = np.where(numpy == id)
            mask[index] = 1

        return np.expand_dims(mask, axis=0)


    # This function computes the identity loss between masked input images and corresponding target images using an embedding network. 
    # The loss is calculated based on the distances between the embeddings of the masked input images and the target images
    def id_loss(self, x_in, targ, embedder):

        id_loss = torch.tensor(0) # initial id_loss tensor

        masked_input = x_in * self.mask # preserve only the masked regions
        # masked_input = x_in

        # resizing to 112
        masked_input = F.interpolate(masked_input, (112, 112))
        targ         = F.interpolate(targ, (112, 112))

        # TODO
        masked_input = self.image_augmentations(masked_input)

        src_id  = embedder(masked_input)
        src_id = F.normalize(src_id, p=2, dim=1)

        targ_id = embedder(targ)
        targ_id = F.normalize(targ_id, p=2, dim=1)

        # The cosine similarity between the normalized embeddings is calculated using cosin_metric (cosine similarity function)
        # and subtracted from 1 to obtain the distances between the embeddings
        dists   = 1 - cosin_metric(src_id, targ_id)

        # For each image in the batch, the average distance between its embedding and the corresponding target embedding is computed. 
        # The average distances are then summed together.
        for i in range(self.args.batch_size):
            id_loss = id_loss + dists[i:: self.args.batch_size].mean()

        # The resulting id_loss tensor represents the cumulative identity loss across the batch
        return id_loss


    def edit_image_by_prompt(self):
        def cond_fn(x, t, img_id, y=None):
            with torch.enable_grad():
                x = x.detach().requires_grad_()

                t = self.unscale_timestep(t)

                # Compute mean and variance using the diffusion model
                out = self.diffusion.p_mean_variance(
                    self.model, x, t, img_id, clip_denoised=False, model_kwargs={"y": y}
                )

                fac = self.diffusion.sqrt_one_minus_alphas_cumprod[t[0].item()]

                # Interpolate between the predicted starting point and input x
                x_in = out["pred_xstart"] * fac + x * (1 - fac)

                loss = torch.tensor(0)

                # ID loss
                targ = self.src_image
                arc_src   = (x_in + 1) / 2
                arc_src   = transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])(arc_src)
                arc_targ  = (targ + 1) / 2
                arc_targ  = transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])(arc_targ)
                id_loss   = self.id_loss(arc_src, arc_targ, self.netArc) * self.args.loss_weight

                loss = loss + id_loss
                self.metrics_accumulator.update_metric("id_loss", id_loss.item())

                # Segmentation loss
                src_mask  = (x_in + 1) / 2
                src_mask  = transforms.Resize((512,512))(src_mask)
                src_mask  = transforms.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225))(src_mask)
                targ_mask = (self.targ_image + 1) / 2
                targ_mask  = transforms.Resize((512,512))(targ_mask)
                targ_mask = transforms.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225))(targ_mask)

                src_seg  = self.netSeg(self.spNorm(src_mask))[0]
                src_seg = transforms.Resize((256, 256))(src_seg)
                targ_seg = self.netSeg(self.spNorm(targ_mask))[0]
                targ_seg = transforms.Resize((256, 256))(targ_seg)

                seg_loss = torch.tensor(0).to(self.device).float()

                # Attributes = [0, 'background', 1 'skin', 2 'r_brow', 3 'l_brow', 4 'r_eye', 5 'l_eye', 6 'eye_g', 7 'l_ear', 8 'r_ear', 9 'ear_r', 10 'nose', 11 'mouth', 12 'u_lip', 13 'l_lip', 14 'neck', 15 'neck_l', 16 'cloth', 17 'hair', 18 'hat']
                ids = [1, 2, 3, 4, 5, 10, 11, 12, 13]

                for id in ids:
                    seg_loss += l1_loss(src_seg[0,id,:,:], targ_seg[0,id,:,:])
                    # seg_loss += mse_loss(src_seg[0,id,:,:], targ_seg[0,id,:,:])

                loss = loss + seg_loss * 200
                self.metrics_accumulator.update_metric("seg_loss", seg_loss.item())

                # Gaze loss
                if t < 50 and t > 10:
                    src_eye = x_in * 0.5 + 0.5
                    targ_eye = self.targ_image
                    targ_eye = targ_eye * 0.5 + 0.5
                    llx, lly, lrx, lry, rlx, rly, rrx, rry = get_eye_coords(self.fa, targ_eye)

                    if llx is not None:
                        targ_left_eye   = targ_eye[:, :, lly:lry, llx:lrx]
                        src_left_eye    = src_eye[:, :, lly:lry, llx:lrx]
                        targ_right_eye  = targ_eye[:, :, rly:rry, rlx:rrx]
                        src_right_eye   = src_eye[:, :, rly:rry, rlx:rrx]
                        targ_left_eye   = torch.mean(targ_left_eye, dim=1, keepdim=True)
                        src_left_eye    = torch.mean(src_left_eye, dim=1, keepdim=True)
                        targ_right_eye  = torch.mean(targ_right_eye, dim=1, keepdim=True)
                        src_right_eye   = torch.mean(src_right_eye, dim=1, keepdim=True)
                        targ_left_gaze  = self.netGaze(targ_left_eye.squeeze(0))
                        src_left_gaze   = self.netGaze(src_left_eye.squeeze(0))
                        targ_right_gaze = self.netGaze(targ_right_eye.squeeze(0))
                        src_right_gaze  = self.netGaze(src_right_eye.squeeze(0))
                        left_gaze_loss  = l1_loss(targ_left_gaze, src_left_gaze)
                        right_gaze_loss = l1_loss(targ_right_gaze, src_right_gaze)
                        gaze_loss = (left_gaze_loss + right_gaze_loss) * 200

                        loss = loss + gaze_loss.sum()
                        self.metrics_accumulator.update_metric("gaze_loss", gaze_loss.item())
                    else:
                        print('no eye detected')

                # Background loss
                masked_background = x_in

                loss = (loss + mse_loss(masked_background, self.targ_image) * 50)
                self.metrics_accumulator.update_metric("l2_loss", mse_loss(masked_background, self.targ_image).item())
                self.metrics_accumulator.update_metric("bg_loss", mse_loss(masked_background, self.targ_image * (1 - self.mask)).item())
                # ------------------------------------------------------------------------------------------------------------------------ #

                return -torch.autograd.grad(loss, x)[0]

        @torch.no_grad() # function should be executed in a no-gradient mode
        # This function adjusts the output by incorporating the mask and background stage
        def postprocess_fn(out, t):

            if self.mask is not None:
                background_stage_t = self.diffusion.q_sample(self.targ_image, t[0])
                background_stage_t = torch.tile(
                    background_stage_t, dims=(self.args.batch_size, 1, 1, 1)
                )

                # The softmask tensor is calculated by multiplying self.mask with a scaling factor (min(1, (75-(t.data+1))/(75.0-self.args.masking_threshold))).
                # This scaling factor gradually reduce the influence of the mask over time, 
                # with the value (75-(t.data+1)) being divided by (75.0-self.args.masking_threshold) and then clamped to a maximum value of 1.
                softmask = self.mask * (min(1, (75-(t.data+1))/(75.0-self.args.masking_threshold)))

                if self.args.enforce_background:
                    out["sample"] = out["sample"] * softmask + background_stage_t * (1 - softmask)
                else:
                    if t > self.args.masking_threshold:
                        out["sample"] = out["sample"] * softmask + background_stage_t * (1 - softmask)

            return out

        # load source images and resize to 256, remember our model was trained on 256 images
        src_dataset  = VGGDataset(path='./data/src/aligned',  img_size=256)
        # load destination images and resize to 256, remember our model was trained on 256 images
        targ_dataset = VGGDataset(path='./data/dst/aligned', img_size=256)
        # Use the Python iterable over the dataset
        src_loader   = DataLoader(src_dataset,  num_workers=4, shuffle=False, batch_size=1)
        targ_loader  = DataLoader(targ_dataset, num_workers=4, shuffle=False, batch_size=1)
        # iterators for src and target
        src_iter     = iter(src_loader)
        targ_iter    = iter(targ_loader)

        # Attributes = [0, 'background', 1 'skin', 2 'r_brow', 3 'l_brow', 4 'r_eye', 5 'l_eye', 6 'eye_g', 7 'l_ear', 8 'r_ear', 9 'ear_r', 10 'nose', 11 'mouth', 12 'u_lip', 13 'l_lip', 14 'neck', 15 'neck_l', 16 'cloth', 17 'hair', 18 'hat']
        # RGB color list for marking different face areas
        color_list = [[0, 0, 0], [255, 0, 0], [0, 204, 204], [0, 0, 204], [255, 153, 51], [204, 0, 204], [0, 0, 0],
                      [204, 0, 0], [102, 51, 0], [0, 0, 0], [76, 153, 0], [102, 204, 0], [255, 255, 0], [0, 0, 153],
                      [0, 0, 0], [0, 0, 0], [0, 0, 0], [0, 0, 0], [0, 0, 0]]

        length = len(src_loader)
        print('Number of Test Data: ', length)
        path = self.args.output_path

        # We will be iterating over each src and targ image for processing
        for step in range(length):
            # save current iteration of src and targ to self
            try:
                self.src_image = next(src_iter).to(self.device).float()
                self.targ_image = next(targ_iter).to(self.device).float()
            except StopIteration:
                src_iter        = iter(src_loader)
                targ_iter       = iter(targ_loader)
                self.src_image  = next(src_iter).to(self.device).float()
                self.targ_image = next(targ_iter).to(self.device).float()

            # Clone the target image for making the mask
            targ_mask = self.targ_image.detach().clone()
            # we resize the mask to 512x512 for manipulation
            targ_mask = transforms.Resize((512,512))(targ_mask)
            # Normalize target mask with mean and standard deviation
            targ_mask = transforms.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225))(targ_mask)
            # Pass the mask through FaceParser (netseg) after applying SpecificNorm (mean and standard deviation) to the mask (from utils/module.py)
            targ_mask = self.netSeg(self.spNorm(targ_mask))[0]
            # Resize the mask back to 256 (original size)
            targ_mask = transforms.Resize((256,256))(targ_mask)

            # coloring the mask according to the specifiec color_list above
            parsing   = targ_mask.squeeze(0).detach().cpu().numpy().argmax(0)
            targ_base = np.zeros((256, 256, 3))
            for idx, color in enumerate(color_list):
                targ_base[parsing == idx] = color
            targ_base /= 255.0

            # making the binary mask
            mask  = self.makeMask(targ_mask)

            # convert the array mask to a PyTorch tensor, adds a batch dimension and converts the data type to float
            self.mask = torch.from_numpy(mask).unsqueeze(0).to(self.device).float()
            # enable gradient tracking for the tensor
            self.mask.requires_grad_()

            # TODO
            self.src_image  = self.src_image  * 2.0 - 1.0
            self.targ_image = self.targ_image * 2.0 - 1.0

            # making output rank and step directories
            self.args.output_path = path + '/' + str(step)
            os.makedirs(self.args.output_path, exist_ok=True)
            self.RankPath = path + '/Rank'+ str(step) + '/'
            os.makedirs(self.RankPath, exist_ok=True)

            # num_timesteps is the number of diffusion steps in the original process to divide up.
            save_image_interval = self.diffusion.num_timesteps // 5
            # Number of applied iteration is defined in iterations_num parameter, default is 4
            for iteration_number in range(self.args.iterations_num):    # self.args.iterations_num: 8
                print(f"Start iterations {iteration_number}")

                # If --ddim argument is provided the sampling function is a DDIM, it is a DDM otherwise (default)
                sample_func = (
                    self.diffusion.ddim_sample_loop_progressive
                    if self.args.ddim
                    else self.diffusion.p_sample_loop_progressive
                )

                # normalize src image with mean and standard deviation
                img  = (self.src_image + 1) / 2
                img  = transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])(img)
                # Down/up sampling the input
                img_id = F.interpolate(img, (112, 112))
                # apply netarc (face recognition) to src image
                img_id = self.netArc(img_id)
                img_id = F.normalize(img_id, p=2, dim=1)

                # Generating samples
                samples = sample_func(
                    self.model,
                    (
                        self.args.batch_size,
                        3,
                        self.model_config["image_size"],
                        self.model_config["image_size"],
                    ),
                    clip_denoised=False,
                    model_kwargs={},
                    cond_fn=cond_fn,
                    progress=True,
                    skip_timesteps=self.args.skip_timesteps,
                    init_image=self.targ_image,
                    postprocess_fn=postprocess_fn,
                    randomize_class=True,
                    img_id = img_id
                )
                intermediate_samples = [[] for i in range(self.args.batch_size)]
                total_steps = self.diffusion.num_timesteps - self.args.skip_timesteps - 1
                
                for j, sample in enumerate(samples):
                    # save image on save_image_interval or when finishing save_image_interval
                    # should_save_image = j % save_image_interval == 0 or j == total_steps

                    # save image only on when finishing steps
                    should_save_image = j == total_steps
                    
                    if should_save_image:
                        self.metrics_accumulator.print_average_metric() # Prints the average metric
                        pred_image = sample["pred_xstart"][0] # Retrieves the predicted image from the sample dictionary
                        # Creates a file path for visualization (the file containing the visualisation progress)
                        visualization_path = Path(os.path.join(self.args.output_path, self.args.output_file))
                        # Modifies the stem of the visualization file path
                        visualization_path = visualization_path.with_stem(f"{visualization_path.stem}_i_{iteration_number}_b_{0}")
                        # Adjusts the pixel values of the predicted image
                        pred_src_mask = pred_image.add(1).div(2).clamp(0, 1)
                        # Resizes the predicted image for applying the model
                        pred_src_mask = transforms.Resize((512, 512))(pred_src_mask.unsqueeze(0))
                        # Normalizes the predicted image
                        pred_src_mask = transforms.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225))(pred_src_mask)
                        # Pass the predicted src mask through FaceParser (netseg) after applying SpecificNorm (mean and standard deviation) to the mask (from utils/module.py)
                        pred_src_mask = self.netSeg(self.spNorm(pred_src_mask))[0]
                        # Resize image back to original 256
                        pred_src_mask = transforms.Resize((256, 256))(pred_src_mask)
                        # Extracts the parsing information from the predicted mask
                        parsing = pred_src_mask.squeeze(0).detach().cpu().numpy().argmax(0)

                        # Creates an empty array for the source base
                        src_base = np.zeros((256, 256, 3))
                         # Iterates over the color list
                        for idx, color in enumerate(color_list):
                            src_base[parsing == idx] = color # Assigns colors based on parsing indices
                        src_base /= 255. # Normalizes the source base

                        # Converts the source base to a torch tensor
                        _src_base = torch.from_numpy(src_base)[:,:,0].to(self.device)
                        # Reshapes the source base tensor
                        _src_base = _src_base.unsqueeze(0).unsqueeze(0)

                        # if we are at the final step
                        if (
                                self.mask is not None
                                and j == total_steps
                        ):
                            if self.args.enforce_background:
                                # Applies the target base as a mask to the predicted image (keep the target background)
                                pred_image = (self.targ_image[0] * (1 - self.mask[0]) + pred_image * self.mask[0])
                            else:
                                # Applies the source base as a mask to the predicted image (keep the source background)
                                pred_image = (self.targ_image[0] * (1 - _src_base[0]) + pred_image * _src_base[0])

                        # Adjusts the pixel values of the predicted image to a PIL image
                        pred_image = pred_image.add(1).div(2).clamp(0, 1)
                        pred_image_pil = TF.to_pil_image(pred_image)

                        # Adjusts the pixel values of the source image to a PIL image
                        src_image_pil = self.src_image[0].add(1).div(2).clamp(0, 1)
                        src_image_pil = TF.to_pil_image(src_image_pil)

                        # Adjusts the pixel values of the target image to a PIL image
                        targ_image_pil = self.targ_image[0].add(1).div(2).clamp(0, 1)
                        targ_image_pil = TF.to_pil_image(targ_image_pil)

                        # Retrieves the mask (generated before) and convert it to a PIL image
                        mask_pil  = self.mask[0]
                        self.mask_pil = TF.to_pil_image(mask_pil)

                        # Compute the ID distance (ID loss) between the predicted image and the source image
                        final_distance = self.id_distance(pred_image_pil, src_image_pil)
                        # Format the distance value as a string
                        formatted_distance = f"{final_distance:.4f}"

                        # In our final step, we do the following
                        if j == total_steps:
                            # Remove dots from distance value to append it as file name
                            path_friendly_distance = formatted_distance.replace(".", "")
                            # Save the predicted image in Rank folder with the modified distance string as the filename
                            pred_image_pil.save(self.RankPath+ str(path_friendly_distance) + '.png')
                            
                            # Save the predicted image in output folder with the iteration number as the filename
                            # pred_image_pil.save(self.args.output_path + '_' + str(iteration_number)+'.png')
                            
                            # Save the predicted image (the unmerged result)
                            pred_image_pil.save("./data/pred.png")

                        # Append the predicted image to the intermediate samples list
                        intermediate_samples[0].append(pred_image_pil)

                        # I think this is unecessary as we are always inside the if
                        # if should_save_image:

                        # This function from (utils/visualization)
                        # It outputs the result grid image we find in the output folder
                        show_editied_masked_image(
                            title='Results',
                            source_image=src_image_pil,
                            target_image=targ_image_pil,
                            edited_image=pred_image_pil,
                            mask=self.mask_pil,
                            targ_parser=targ_base,
                            src_parser=src_base,
                            path=visualization_path,
                            distance=formatted_distance,
                        )
