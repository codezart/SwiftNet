# Imports
from __future__ import division

import argparse
import os
import random
import logging

import torch
import torch.nn as nn
import torch.nn.functional as F
from datetime import datetime
from PIL import Image
from torch.utils import data

from dataset.davis import DAVIS_MO_Train
from dataset.youtube import Youtube_MO_Train
# Custom Libs
from davis import \
    DAVIS_MO_Test  # this seems to be the most updated one so using it
from eval import evaluate  # for test loading davis test
from swiftnet import SwiftNet

# This flag allows you to enable the inbuilt cudnn auto-tuner to find the best algorithm to use for your hardware
torch.backends.cudnn.benchmark = True

def get_arguments():
    """This function gets all the arguments from the python command"""

    parser = argparse.ArgumentParser(description="SST")

    parser.add_argument(
        "-Ddavis",
        type=str,
        help="path to data",
        default="~/Downloads/DAVIS/",
    )
    parser.add_argument(
        "-Dyoutube",
        type=str,
        help="path to youtube-vos",
        default="~/Downloads/YOUTUBEVOS/",
    )
    parser.add_argument("-batch", type=int, help="batch size", default=4)
    parser.add_argument(
        "-max_skip", type=int, help="max skip between training frames", default=25
    )
    parser.add_argument(
        "-change_skip_step", type=int, help="change max skip per x iter", default=3000
    )
    parser.add_argument("-total_iter", type=int, help="total iter num", default=800000)
    parser.add_argument(
        "-test_iter", type=int, help="evaluate per x iters", default=10000
    )
    parser.add_argument("-log_iter", type=int, help="log per x iters", default=500)
    parser.add_argument(
        "-resume_path",
        type=str,
        default="/smart/haochen/cvpr/weights/coco_pretrained_resnet50_679999.pth",
    )
    parser.add_argument("-save", type=str, default="../weights")
    parser.add_argument("-sample_rate", type=float, default=0.08)
    parser.add_argument(
        "-backbone",
        type=str,
        help="backbone ['resnet50', 'resnet18']",
        default="resnet50",
    )

    return parser.parse_args()


def adjust_learning_rate(iteration, total_iter, power=0.9):
    return 1e-5 * pow((1 - 1.0 * iteration / total_iter), power)


def main():

    # get and store arguments
    args = get_arguments()
    rate = args.sample_rate
    DAVIS_ROOT = args.Ddavis
    YOUTUBE_ROOT = args.Dyoutube
    pth_path = args.resume_path
    total_iter = args.total_iter
    accumulation_step = args.batch
    save_step = args.test_iter
    log_iter = args.log_iter
    change_skip_step = args.change_skip_step

    logging.info("Saved all arguments")

    # get sample palette for DAVIS mask
    palette = Image.open(DAVIS_ROOT + "/Annotations/480p/blackswan/00000.png").getpalette()

    logging.info("Saved DAVIS mask palette")

    # DAVIS Train Get data.Dataset and add to dataloader to get iter
    davis_trainset = DAVIS_MO_Train(
        DAVIS_ROOT,
        resolution="480p",
        imset="20{}/{}.txt".format(17, "train"),
        single_object=False,
    )
    davis_trainloader = data.DataLoader(
        davis_trainset, batch_size=1, num_workers=1, shuffle=True, pin_memory=True
    )
    davis_trainloader_iter = iter(davis_trainloader)

    logging.info("Acquired DAVIS train dataset")

    # Youtube Train Get data.Dataset and add to dataloader to get iter
    youtube_trainset = Youtube_MO_Train("{}train/".format(YOUTUBE_ROOT))
    youtube_trainloader = data.DataLoader(
        youtube_trainset, batch_size=1, num_workers=1, shuffle=True, pin_memory=True
    )
    youtube_trainloader_iter = iter(youtube_trainloader)

    logging.info("Acquired Youtube train dataset")

    # Davis Test get data.Dataset 
    davis_testloader = DAVIS_MO_Test(
        DAVIS_ROOT,
        resolution="480p",
        imset="20{}/{}.txt".format(17, "val"),
        single_object=False,
    )

    logging.info("Acquired DAVIS test dataset")

    # initialize model with dataparallel for multi gpu processing
    model = nn.DataParallel(SwiftNet())
    logging.info("Loading weights: ", pth_path)

    # load the pretrained model
    model.load_state_dict(
        torch.load(pth_path),
        strict=False
    )

    logging.info("loaded pretrain model {}".format(pth_path)) 

    # make model cuda if cuda available
    if torch.cuda.is_available():
        model.cuda() # moves model's parameters and buffers to the GPU
        print("CUDA set") 

    # set model to training mode
    model.train()

    # set batchNorm layers to evaluation mode
    for module in model.modules():
        if isinstance(module, torch.nn.modules.BatchNorm1d):
            module.eval()
        if isinstance(module, torch.nn.modules.BatchNorm2d):
            module.eval()
        if isinstance(module, torch.nn.modules.BatchNorm3d):
            module.eval()

    # set loss type
    criterion = nn.CrossEntropyLoss() # commonly used in training classification models
    criterion.cuda()

    # set optimizer
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-5, eps=1e-8, betas=[0.9, 0.999])

    loss_momentum = 0
    max_skip = 25
    skip_n = 0
    max_jf = 0

    # train model total_iter
    for iter_ in range(total_iter):
        
        # adjust learning rate every 1000 iterations
        if (iter_ + 1) % 1000 == 0:
            lr = adjust_learning_rate(iter_, total_iter)
            for param_group in optimizer.param_groups:
                param_group["lr"] = lr
        
        # change skip
        if (iter_ + 1) % change_skip_step == 0:
            logging.info("Entered change skip") 

            if skip_n < max_skip:
                skip_n += 1
            
            youtube_trainset.change_skip(skip_n // 5)
            youtube_trainloader_iter = iter(youtube_trainloader)

            davis_trainset.change_skip(skip_n)
            davis_trainloader_iter = iter(davis_trainloader)

        # based on a rate get the next set of frames, masks, num_objects, info from either DAVIS or YOUTUBEVOS
        if random.random() < rate:
            try:
                logging.info("Getting DAVIS data Fs Ms etc") 
                Fs, Ms, num_objects, info = next(davis_trainloader_iter)
            except:
                davis_trainloader_iter = iter(davis_trainloader)
                Fs, Ms, num_objects, info = next(davis_trainloader_iter)
                logging.info("Getting next failed, restarting with iter") 
        else:
            try:
                logging.info("Getting Youtube data Fs Ms etc") 
                Fs, Ms, num_objects, info = next(youtube_trainloader_iter)
            except:
                youtube_trainloader_iter = iter(youtube_trainloader)
                Fs, Ms, num_objects, info = next(youtube_trainloader_iter)
                logging.info("Getting next failed, restarting with iter") 

        logging.info("Fs or image frames: \nFs.shape:{}\nMs.shape:{}\nnum_objects: {}\nInfo: {}".format(Fs.shape,Ms.shape,num_objects,info))
        # seq_name = info["name"][0]
        # num_frames = info["num_frames"][0].item()
        # num_frames = 3

        # create empty mask tensor
        Es = torch.zeros_like(Ms) # size of Ms: torch.size([1, 11, 3, 384, 384]) (could be like this: (batch_size, num_objects_in_frame, channels, height, width))
        Es[:, :, 0] = Ms[:, :, 0] # copy first frame's mask
        logging.info("Es mask empty tensor size {}\n Es[:,:,0].size: {},\n".format(Es.size(),  Es[:,:,0].size()))

        n1_key, n1_value = model(
            Fs[:, :, 0], 
            Es[:, :, 0], 
            None, 
            None, 
            None, 
            None, 
            torch.tensor([num_objects]),
            first_frame_flag = True
        )
        n2_logit, r4, r3, r2, c1 = model(
            Fs[:, :, 0], 
            n1_key, 
            n1_value, 
            torch.tensor([num_objects])
        )
        n2_label = torch.argmax(Ms[:, :, 1], dim=1).long().cuda()
        n2_loss = criterion(n2_logit, n2_label)
        
        Es[:, :, 1] = F.softmax(n2_logit, dim=1)

        n2_key, n2_value = model(
            Fs[:, :, 1], 
            Es[:, :, 1],
            r4, 
            r3, 
            r2, 
            c1, 
            num_objects
        )
        n12_keys = torch.cat([n1_key, n2_key], dim=2)
        n12_values = torch.cat([n1_value, n2_value], dim=2)
        n3_logit, r4, r3, r2, c1 = model(
            Fs[:, :, 2], 
            n12_keys, 
            n12_values, 
            num_objects
        )

        n3_label = torch.argmax(Ms[:, :, 2], dim=1).long().cuda()
        n3_loss = criterion(n3_logit, n3_label)
        Es[:, :, 2] = F.softmax(n3_logit, dim=1)

        loss = n2_loss + n3_loss
        # loss = loss / accumulation_step

        loss.backward()
        loss_momentum += loss.cpu().data.numpy()

        if (iter_ + 1) % accumulation_step == 0:
            optimizer.step()
            optimizer.zero_grad()

        if (iter_ + 1) % log_iter == 0:
            print(
                "iteration:{}, loss:{}, remaining iteration:{}".format(
                    iter_, loss_momentum / log_iter, args.total_iter - iter_
                )
            )
            loss_momentum = 0

        if (iter_ + 1) % save_step == 0 and (iter_ + 1) >= 600000:
            if not os.path.exists(args.save):
                os.makedirs(args.save)
            torch.save(
                model.state_dict(),
                os.path.join(
                    args.save, "davis_youtube_{}_{}.pth".format(args.backbone, str(iter_))
                ),
            )

            model.eval()

            print("Evaluate at iter: " + str(iter_))
            g_res = evaluate(model, davis_testloader, ["J", "F"])

            if g_res[0] > max_jf:
                max_jf = g_res[0]

            print("J&F: " + str(g_res[0]), "Max J&F: " + str(max_jf))

            model.train()
            for module in model.modules():
                if isinstance(module, torch.nn.modules.BatchNorm1d):
                    module.eval()
                if isinstance(module, torch.nn.modules.BatchNorm2d):
                    module.eval()
                if isinstance(module, torch.nn.modules.BatchNorm3d):
                    module.eval()

if __name__ == "__main__":
    now = datetime.now()
    logging.basicConfig(filename='train{}.log'.format(now.strftime("_%Y_%m_%d_%H_%M_%S")), filemode='a', level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
    main()