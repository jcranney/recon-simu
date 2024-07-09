#!/usr/bin/env python3

import numpy as np
import aotools
from pydantic import BaseModel, ConfigDict
from tqdm import tqdm
import time
from typing import Union
from pyMilk.interfacing.isio_shmlib import SHM

if __name__ == "__main__":
    pup_width = 64
    fovx = 8 # pixels
    nsubx = 32 # across diameter
    pupil = aotools.circle(pup_width//2, pup_width).astype(bool)

    shm_suffix = "-scaosim"
    shm_atmos = SHM("turb"+shm_suffix)
    shm_recon = SHM("recon"+shm_suffix)

    pbar = tqdm()
    fps = 30
    t = time.time()
    while True:
        phi_atmos = shm_atmos.get_data()
        phi_recon = shm_recon.get_data()
        residual = phi_atmos - phi_recon
        residual *= pupil
        while True:
            if time.time() - t > 1/fps:
                t = time.time()
                break
            else:
                time.sleep((1/fps)/100)
        pbar.set_description(f"rms wf: {residual[pupil==1].std():0.4f} um")
        pbar.update()