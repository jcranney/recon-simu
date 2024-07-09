#!/usr/bin/env python3

import numpy as np
from tqdm import tqdm
import time
from pyMilk.interfacing.isio_shmlib import SHM

if __name__ == "__main__":
    pup_width = 64  # 
    nsubx = 32  # subapertures across diameter
    
    shm_suffix = "-scaosim"
    shm_slopes = SHM("slopes"+shm_suffix) # read slopes
    shm_recon = SHM("recon"+shm_suffix,((pup_width,pup_width),np.float32)) # write recon
    shm_valid = SHM("validsubaps"+shm_suffix) # read valid subaperture mask
    valid = shm_valid.get_data()
    x_valid,y_valid = np.meshgrid(np.arange(nsubx),np.arange(nsubx),indexing="xy")
    
    # x and y coordinates in same order as slopes from hardware simulator
    x_valid = x_valid[valid==1]
    y_valid = y_valid[valid==1]
    
    def reconstruct_phi(slopes):
        phi_recon = np.zeros([pup_width,pup_width],dtype=np.float32)
        return phi_recon

    pbar = tqdm()
    fps = 100
    t = time.time()
    while True:
        s = shm_slopes.get_data()
        phi = reconstruct_phi(s)
        shm_recon.set_data(phi)
        pbar.set_description(f"slopes std: {s.std():0.4f} arcsec")
        pbar.update()
        
        while True:
            if time.time() - t > 1/fps:
                t = time.time()
                break
            else:
                time.sleep((1/fps)/100)