#!/usr/bin/env python3

import numpy as np
import aotools
from pydantic import BaseModel, ConfigDict
from tqdm import tqdm
import time
from typing import Union
from pyMilk.interfacing.isio_shmlib import SHM
import aocov

class PhaseScreen(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    pupil: np.ndarray
    r0: float = 0.15  # metres at 0.5 micron
    L0: float = 25.0  # metres
    diam: float = 8.0  # metres
    laminar: float = 0.995  # lamination factor
    wind: np.ndarray = np.r_[10, 20]  # [x,y] m/s
    ittime: float = 1/500  # seconds
    pixsize: float = None
    thresh: float = 1e-5  # eigenval threshold
    xx_max: int = None
    vv_max: int = None
    factor_xx: np.ndarray = None
    factor_vv: np.ndarray = None
    x: np.ndarray = None
    seed: int = 1234
    rng: np.random.Generator = None

    class StateMatrix:
        """Special class for the state matrix, since I realised it has some
        really nice properties that allow us to do multiplication ~20x 
        faster.
        """
        def __init__(self, cov_yx, inv_factor_xx):
            self.ML = np.einsum("ij,jk->ik", cov_yx, inv_factor_xx)
            self.LT = inv_factor_xx.T.copy()
            self.A = np.einsum("ij,jk->ik", self.ML, self.LT)
            x = np.ones(self.A.shape[1])
            self.es_path_factored = np.einsum_path(
                "ij,jk,k...->i...",
                self.ML,
                self.LT,
                x,
                optimize="optimal"
            )
            self.es_path_classic = np.einsum_path(
                "ij,j...->i...", 
                self.A, x, optimize="optimal"
            )
            res = self.test_speed(10)
            if res["classic"] > res["factored"]:
                # classic was slower, use factored
                self.dot = self.dot_factored
            else:
                # factored was slower, use classic
                self.dot = self.dot_classic

        def dot_classic(self, x):
            return np.einsum(
                "ij,j...->i...",
                self.A, x, optimize=self.es_path_classic[0])
        
        def dot_factored(self, x):
            return np.einsum(
                "ij,jk,k...->i...",
                self.ML,
                self.LT,
                x,
                optimize=self.es_path_factored[0])
        
        @property
        def shape(self):
            return self.A.shape

        def test_speed(self, ntests=100, seed=1):
            rng = np.random.default_rng(seed)
            x = rng.normal(size=[self.shape[1],ntests])
            t1 = time.time()
            self.dot_factored(x)
            t2 = time.time()
            self.dot_classic(x)
            t3 = time.time()
            print(f"classic:  {(t3-t2)/ntests:0.3e}")
            print(f"factored: {(t2-t1)/ntests:0.3e}")
            return {
                "classic": (t3-t2)/ntests,
                "factored": (t2-t1)/ntests
                }

    state_matrix: StateMatrix = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.rng = np.random.default_rng(self.seed)

        self.pixsize = self.diam / self.pupil.shape[0]
        yy, xx = np.mgrid[:pup_width, :pup_width]*self.pixsize
        yy = yy[self.pupil == 1]
        xx = xx[self.pupil == 1]

        # let sigma_xx -> covariance of phase with self
        # let sigma_yx -> covariance between phase and next phase
        # let sigma_vv -> covariance of driving noise with self
        sigma_xx = self._covariance(
            xx, yy, xx, yy
        )
        self.factor_xx, inv_factor_xx = self._factorh(sigma_xx, self.xx_max)

        sigma_yx = self.laminar * self._covariance(
            xx+self.wind[0]*self.ittime, yy+self.wind[1]*self.ittime, xx, yy
        )
        state_matrix = self.StateMatrix(sigma_yx, inv_factor_xx)
        sigma_vv = sigma_xx - state_matrix.dot(state_matrix.dot(sigma_xx).T).T
        self.state_matrix = state_matrix
        self.factor_vv, _ = self._factorh(sigma_vv, self.vv_max)
        self.x = self.factor_xx @ self.rng.normal(size=self.factor_xx.shape[1])

    def _covariance(self, x_in, y_in, x_out, y_out):
        cov = aocov.phase_covariance_xyxy(
            x_out, y_out, x_in, y_in,
            self.r0, self.L0
            )*(0.5/(np.pi*2))**2
        return cov

    def _factorh(self, symmetric_matrix, n_modes=None):
        vals, vecs = np.linalg.eigh(symmetric_matrix)
        if n_modes is None:
            valid = vals > self.thresh
        else:
            valid = vals >= vals[vals.argsort()[-n_modes]]
        vecs = vecs[:, valid]
        vals = vals[valid]
        factor = vecs * (vals**0.5)[None,:]
        inv_factor = vecs * ((1/vals)**0.5)[None,:]
        return factor, inv_factor

    def step(self):
        v = self.rng.normal(size=self.factor_vv.shape[1])
        self.x = self.state_matrix.dot(self.x) + self.factor_vv @ v

    @property
    def phase(self):
        phi = np.zeros(self.pupil.shape)
        phi[self.pupil] = self.x.copy()
        return phi.astype(np.float32)


class SHWFS(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    pupil: np.ndarray
    nsubx: int = 32  # number of subapertures across diameter
    fovx: int = 8  # pixels per fov width
    wavelength: float = 0.589  # sensing wavelength in microns
    dft2: np.ndarray = None
    slices: list = None
    es_path: tuple = None
    _im_subaps: np.ndarray = None
    _im_full: np.ndarray = None
    diam: float = 8.0 # metre telescope
    padded_width: float = None

    @property
    def pixel_scale(self):
        return (self.wavelength*1e-6) / (self.diam/self.nsubx) * self.subwidth/self.padded_width / 4.848e-6

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        padded_width = np.max([self.subwidth*2, self.fovx])
        self.padded_width = padded_width
        dft = np.fft.fft(np.eye(padded_width), norm="ortho")
        dft = np.fft.fftshift(dft, axes=[0])
        dft = dft[:, :self.subwidth]*np.exp(-1j*np.pi*2*np.arange(self.subwidth)/self.padded_width/2)[None,:]
        if self.fovx < padded_width:
            dft = dft[
                padded_width//2-self.fovx//2:padded_width//2+self.fovx//2
            ]
        dft2 = np.kron(dft, dft)
        self.dft2 = dft2
        
        camp = pupil.astype(np.complex128)
        # reshape camp so that it's batched into a 4d array with shape:
        #   (nsub, nsub, subwidth, subwidth)
        camp = camp.reshape(self.nsubx, self.subwidth, self.nsubx, self.subwidth).swapaxes(1, 2)
        # flatten the `subwidth` dimensions:
        camp = camp.reshape(self.nsubx, self.nsubx, self.subwidth*self.subwidth)
        # get the optimal einsum path to use online
        self.es_path = tuple(
            np.einsum_path("ijq,pq->ijp",camp,self.dft2,optimize="optimal")
        )
    
    def measure(self, phi):
        """Measure phase `phi` with shwfs.

        Takes `phi` in microns, returns wfs image intensity
        """
        # I'm aware that this is basically unreadable, but it's hella fast.
        
        # compute complex amplitude from phase and pupil
        camp = pupil.astype(np.complex128) * \
            np.exp(1j*phi*2*np.pi/self.wavelength)
        
        # reshape camp so that it's batched into a 4d array with shape:
        #   (nsub, nsub, subwidth, subwidth)
        camp = camp.reshape(self.nsubx, self.subwidth, self.nsubx, self.subwidth).swapaxes(1, 2)
        # flatten the phase dimension:
        camp = camp.reshape(self.nsubx, self.nsubx, self.subwidth*self.subwidth)
        # do the fft2's batched using the MVM (DFT2) method
        im = np.einsum("ijq,pq->ijp",camp,self.dft2,optimize=self.es_path[0])
        # convert camplitude to intensity
        im = np.abs(im)**2
        # save a view of the image batched into subapertures (for the centroider)
        self._im_subaps = im.reshape(self.nsubx*self.nsubx, self.fovx, self.fovx)
        # reshape into something that looks like a wfs image
        im = im.reshape(self.nsubx, self.nsubx, self.fovx, self.fovx).swapaxes(1, 2)
        im = im.reshape(self.nsubx * self.fovx, self.nsubx * self.fovx)
        # save a view of the image as a full WFS readout
        self._im_full = im

    @property
    def subwidth(self):
        return self.pupil.shape[0] // self.nsubx
    
    @property
    def image(self):
        return self._im_full.copy()

    @property
    def image_batched(self):
        return self._im_subaps.copy()


class ClassicCog(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    npix : int
    r_mat : np.ndarray = None
    xy_mat : np.ndarray = None
    thresh : Union[None, float] = 0.0
    einsum_num_str : str = "jk,ik,jk->ij"
    es_path : list = None

    def calibrate(self):
        x_span = np.arange(self.npix)-self.npix/2+0.5
        xx,yy = np.meshgrid(x_span,x_span,indexing="xy")
        # xy matrix is [2,Npixels]
        self.xy_mat = np.concatenate([
            xx.flatten()[:,None],
            yy.flatten()[:,None]
            ],axis=1).T
        self.r_mat = np.ones(self.xy_mat.shape)
        self.optimize_einsum()

    def optimize_einsum(self, batch_size=1):
        intsty = np.zeros([batch_size, self.npix*self.npix])
        self.es_path = np.einsum_path(
            self.einsum_num_str,
            self.r_mat,
            intsty,
            self.xy_mat,
            optimize = self.es_path
        )

    def cog(self, intensity):
        """compute the centroid of an [npix,npix] image or batch of 
        [N,npix,npix] images.
        """
        if len(intensity.shape)==2:
            intensity = intensity[None,...]
        if self.thresh is not None:
            intensity = intensity - self.thresh
            intensity[intensity < 0] = 0.0
        num = np.einsum(
            self.einsum_num_str,
            self.r_mat,
            intensity.reshape(intensity.shape[0], np.prod(intensity.shape[1:])),
            self.xy_mat,
            optimize = self.es_path[0]
        )
        den = np.sum(intensity, axis=(1,2))[:,None]
        return  (num / den).astype(np.float32)


if __name__ == "__main__":
    pup_width = 64
    fovx = 8 # pixels
    nsubx = 32 # across diameter
    pupil = aotools.circle(pup_width//2, pup_width).astype(bool)

    shm_suffix = "-scaosim"

    shm_phase = SHM("turb"+shm_suffix,((pup_width,pup_width),np.float32))

    # for now, just one phase screen, eventually this will be wrapped
    # by an atmosphere object, which combines the turbulence sensibly
    # for use in tomographic systems.
    phasescreen = PhaseScreen(
        pupil=pupil,
        thresh=1e-3, # (e.g) 1e-2 -> poor detail, 1e-10 -> fine detail
        laminar=0.999,
        r0 = 0.2,
        xx_max = 500 # only propagate the first 500 modes in the state, for speed
    )

    shwfs = SHWFS(pupil=pupil, nsubx=nsubx, fovx=fovx)
    phi = phasescreen.phase
    shwfs.measure(phi)
    im = shwfs.image

    cog = ClassicCog(
        npix=fovx,
        thresh=0.0,
    )
    cog.calibrate()

    slopes = cog.cog(shwfs.image_batched)

    flux = shwfs.image_batched.sum(axis=(1,2))
    
    valid = flux > (0.9*flux.max())
    
    shm_slopes = SHM("slopes"+shm_suffix,((valid.sum()*2,),np.float32))
    shm_valid = SHM("validsubaps"+shm_suffix,((nsubx,nsubx),np.uint8))
    shm_valid.set_data(valid.astype(np.uint8).reshape([nsubx,nsubx]))
    shm_wfsim = SHM("wfsimg"+shm_suffix,(shwfs.image.shape,np.float32))

    pbar = tqdm()
    while True:
        phasescreen.step()
        phi = phasescreen.phase
        shwfs.measure(phi)
        slopes = cog.cog(shwfs.image_batched)[valid].T.flatten() # yao slope fmt
        slopes *= shwfs.pixel_scale
        shm_phase.set_data(phi)
        shm_slopes.set_data(slopes)
        shm_wfsim.set_data(shwfs.image.astype(np.float32))
        pbar.set_description(f"rms wf: {phi[pupil==1].std():0.4f} um")
        pbar.update()