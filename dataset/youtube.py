import os
import os.path as osp
import numpy as np
from PIL import Image

import torch
import torchvision
from torch.utils import data
import random
import glob
import pdb
import cv2
from dataset.aug import aug_heavy

MAX_OBJECT_NUM_PER_SAMPLE = 5


class Youtube_MO_Train(data.Dataset):
    # Dataset class for training on multi-object data from YouTube videos.
    # for multi object, do shuffling

    def __init__(self, root):
        self.root = root # Root directory for dataset
        self.mask_dir = os.path.join(root, "Annotations") # Directory containing mask annotations
        self.image_dir = os.path.join(root, "JPEGImages") # Directory containing images

        # List video folders found in image directory
        self.videos = [
            i.split("/")[-1] for i in glob.glob(os.path.join(self.image_dir, "*"))
        ]

        # Dictionaries to store the number of frames, image file paths, and mask file paths for each video
        self.num_frames = {}
        self.img_files = {}
        self.mask_files = {}

        # Populate the above dictionaries for each video
        for _video in self.videos:
            tmp_imgs = glob.glob(os.path.join(self.image_dir, _video, "*.jpg"))
            tmp_masks = glob.glob(os.path.join(self.mask_dir, _video, "*.png"))
            tmp_imgs.sort()
            tmp_masks.sort()
            self.img_files[_video] = tmp_imgs
            self.mask_files[_video] = tmp_masks
            self.num_frames[_video] = len(tmp_imgs)

        self.K = 11 # Total classes: 10 objects + 1 background # K represents a const used for handling one hot encoding masks. Implying that this dataset is designed to work with up to 10 distinct objects per sample, plus an additional category for background
        self.skip = 0 # Skip factor for selecting frames
        self.aug = aug_heavy() # Data augmentation function

    def __len__(self):
        # Returns the total number of videos
        return len(self.videos)

    def change_skip(self, f):
        # Allows dynamic adjustment of the skip factor
        self.skip = f

    def To_onehot(self, mask):
        # Convert a single mask to a one-hot encoded tensor
        M = np.zeros((self.K, mask.shape[0], mask.shape[1]), dtype=np.uint8)
        for k in range(self.K):
            M[k] = (mask == k).astype(np.uint8)
        return M

    def All_to_onehot(self, masks):
        # Apply one-hot encoding to a batch of masks
        Ms = np.zeros(
            (self.K, masks.shape[0], masks.shape[1], masks.shape[2]), dtype=np.uint8
        )
        for n in range(masks.shape[0]):
            Ms[:, n] = self.To_onehot(masks[n])
        return Ms

    def mask_process(self, mask, f, num_object, ob_list):
        # Process each mask, updating the number of objects and the object list
        n = num_object
        mask_ = np.zeros(mask.shape).astype(np.uint8)
        if f == 0: # if first frame go through pixels which are mask objects and add to object list
            for i in range(1, 11):
                if np.sum(mask == i) > 0:
                    n += 1
                    ob_list.append(i)
            if n > MAX_OBJECT_NUM_PER_SAMPLE: # if unique objects are more than max, remove some
                n = MAX_OBJECT_NUM_PER_SAMPLE
                ob_list = random.sample(ob_list, n)
        for i, l in enumerate(ob_list): # relable object list with i+1 for each object in list [1,2,3,..]
            mask_[mask == l] = i + 1
        return mask_, n, ob_list

    def __getitem__(self, index):
        # Fetches and processes data for a given index (video)
        video = self.videos[index]
        img_files = self.img_files[video]
        mask_files = self.mask_files[video]
        info = {}
        info["name"] = video
        info["num_frames"] = self.num_frames[video]
        # info['size_480p'] = self.size_480p[video]

        # Pre-allocate tensors for frames and masks
        N_frames = np.empty(
            (3,) # could be batch size
            + (
                384,  # image hxw
                384,
            )
            + (3,), # could be color channels
            dtype=np.float32,
        )
        N_masks = np.empty(
            (3,)
            + (
                384,
                384,
            ),
            dtype=np.uint8,
        )
        frames_ = []
        masks_ = []
        # select first frame randomly from 0 to 3rd last
        n1 = random.sample(range(0, self.num_frames[video] - 2), 1)[0]
        # select second frame randomly from n1 to the next
        n2 = random.sample(
            range(n1 + 1, min(self.num_frames[video] - 1, n1 + 2 + self.skip)), 1
        )[0]
        # same with third frame
        n3 = random.sample(
            range(n2 + 1, min(self.num_frames[video], n2 + 2 + self.skip)), 1
        )[0]

        # Randomly select three frames for processing
        frame_list = [n1, n2, n3]
        num_object = 0
        ob_list = []
        for f in range(3):
            img_file = img_files[frame_list[f]]
            tmp_frame = np.array(Image.open(img_file).convert("RGB"))
            try:
                mask_file = mask_files[frame_list[f]]
                tmp_mask = np.array(Image.open(mask_file).convert("P"), dtype=np.uint8)
            except:
                tmp_mask = 255

            h, w = tmp_mask.shape
            if h < w:
                tmp_frame = cv2.resize(
                    tmp_frame, (int(w / h * 480), 480), interpolation=cv2.INTER_LINEAR
                )
                tmp_mask = Image.fromarray(tmp_mask).resize(
                    (int(w / h * 480), 480), resample=Image.NEAREST
                )
            else:
                tmp_frame = cv2.resize(
                    tmp_frame, (480, int(h / w * 480)), interpolation=cv2.INTER_LINEAR
                )
                tmp_mask = Image.fromarray(tmp_mask).resize(
                    (480, int(h / w * 480)), resample=Image.NEAREST
                )

            frames_.append(tmp_frame)
            masks_.append(np.array(tmp_mask))

        # Load, resize, and augment selected frames and masks
        frames_, masks_ = self.aug(frames_, masks_)

        for f in range(3):
            masks_[f], num_object, ob_list = self.mask_process(
                masks_[f], f, num_object, ob_list
            )
            N_frames[f], N_masks[f] = frames_[f], masks_[f]

        # process masksa and prepare tensors for model input
        Fs = torch.from_numpy(
            np.transpose(N_frames.copy(), (3, 0, 1, 2)).copy()
        ).float()
        Ms = torch.from_numpy(self.All_to_onehot(N_masks).copy()).float()

        if num_object == 0:
            num_object += 1
            
        # Ensure there is at least one object
        num_objects = torch.LongTensor([num_object])
        return Fs, Ms, num_objects, info


if __name__ == "__main__":
    from helpers import overlay_davis
    import matplotlib.pyplot as plt
    import os
    import pdb

    dataset = Youtube_MO_Train("/smart/haochen/cvpr/data/YOUTUBE-VOS/train/")
    dataset.skip = 10
    palette = Image.open(
        "/smart/haochen/cvpr/data/DAVIS/Annotations/480p/blackswan/00000.png"
    ).getpalette()

    output_dir = "tmp"
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    for i, (Fs, Ms, num_objects, info) in enumerate(dataset):
        pred = np.argmax(Ms.numpy(), axis=0).astype(np.uint8)
        img_list = []
        for f in range(3):
            pF = (Fs[:, f].permute(1, 2, 0).numpy() * 255.0).astype(np.uint8)
            pE = pred[f]
            canvas = overlay_davis(pF, pE, palette)
            img = np.concatenate([pF, canvas], axis=0)
            img_list.append(img)
        out_img = np.concatenate(img_list, axis=1)
        out_img = Image.fromarray(out_img)
        out_img.save(os.path.join(output_dir, str(i).zfill(5) + ".jpg"))
        pdb.set_trace()
