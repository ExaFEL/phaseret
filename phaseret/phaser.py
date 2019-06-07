import numpy as np
from scipy.ndimage import gaussian_filter

# try:
#     import cupy as cp
# except ImportError:
#     cp = None
cp = None

# Convention:
#   a trailing underscore in a numpy array means it is
#   assumed to be shifted for the FFT.


class InitialState:
    """Initial State for the Phaser.

    Allow to set up the initial state of the Phaser.
    In particular, it can be convenient to generate the initial
    support and rho (density estimate).
    """

    def __init__(self, amplitudes, support=None, rho=None,
                 is_ifftshifted=False):
        """Creates the InitialState object.

        :param amplitudes:
            Numpy array with amplitude data
        :param support:
            Numpy array with initial support.
            If None, can be generated by using the relevant functions.
            If left to None when required, one will be automatically
            generated.
        :param rho:
            Numpy array with initial density estimate.
            If None, can be generated by using the relevant functions.
            If left to None when required, one will be automatically
            generated.
        :param is_ifftshifted:
            If True, assume that the arrays have been shifted for the
            fft.
        :return:
        """
        shift = np.fft.ifftshift if not is_ifftshifted else lambda x: x

        self._amplitudes_ = shift(amplitudes)
        self._shape = self._amplitudes_.shape

        if support is not None:
            self.check_array(support)
            self._support_ = shift(support)
        else:
            self._support_ = None

        if rho is not None:
            self.check_array(rho)
            self._rho_ = shift(rho)
        else:
            self._rho_ = None

    def check_array(self, array):
        if array.shape != self._shape:
            raise ValueError(
                "All provided arrays need to have the same shape")

    def generate_support_from_autocorrelation(self, rel_threshold=0.01):
        intensities_ = self._amplitudes_ ** 2
        intensities_[np.isnan(intensities_)] = 0
        autocorrelation_ = np.absolute(np.fft.fftn(intensities_))
        self._support_[:] = \
            autocorrelation_ > rel_threshold * autocorrelation_.max()

    def generate_random_rho(self):
        support = self.get_support(ifftshifted=True)  # In case it's None
        self._rho_[:] = support * np.random.rand(*support.shape)

    def get_amplitudes(self, ifftshifted=False):
        shift = np.fft.fftshift if not ifftshifted else lambda x: x
        return shift(self._amplitudes_)

    def get_support(self, ifftshifted=False):
        if self._support_ is None:
            self.generate_support_from_autocorrelation()
        shift = np.fft.fftshift if not ifftshifted else lambda x: x
        return shift(self._support_)

    def get_rho(self, ifftshifted=False):
        if self._rho_ is None:
            self.generate_random_rho()
        shift = np.fft.fftshift if not ifftshifted else lambda x: x
        return shift(self._rho_)


class Phaser:
    """Phasing engine.

    Convenience wrapper around phasing functions.
    """

    def __init__(self, initial_state, device="auto", monitor=False):
        """Initializes the Phaser.

        :param initial_state:
            Object of type InitialState.
        :param device:
            Target device.
            Acceptable values: "cpu", "gpu", or "auto" (default).
            If "auto", use "gpu" if available, "cpu" otherwise.
        :return:
        """
        if device not in ("auto", "gpu", "cpu"):
            raise ValueError("Unknown device: {}".format(device))

        if device == "auto":
            device = "gpu" if cp else "cpu"
        elif device == "gpu":
            if not cp:
                raise GPUNotAvailabeError

        self._is_gpu = device == "gpu"
        self._xp = cp if self._is_gpu else np

        self._amplitudes_ = self._xp.asarray(
            initial_state.get_amplitudes(ifftshifted=True))
        self._support_ = self._xp.asarray(
            initial_state.get_support(ifftshifted=True))
        self._rho_ = self._xp.asarray(
            initial_state.get_rho(ifftshifted=True))

        self._amp_mask_ = self._xp.isfinite(self._amplitudes_)
        self._n_values = self._amp_mask_.sum()

        self._monitor = monitor
        self._distF_l = []
        self._distR_l = []
        self._suppS_l = []

    def ER_loop(self, n_loops):
        for k in range(n_loops):
            self.ER()

    def HIO_loop(self, n_loops, beta):
        for k in range(n_loops):
            self.HIO(beta)

    def ER(self):
        rho_mod_, support_star_ = self._phase()
        self._rho_[:] = self._xp.where(support_star_, rho_mod_, 0)

    def HIO(self, beta):
        rho_mod_, support_star_ = self._phase()
        self._rho_[:] = self._xp.where(support_star_, rho_mod_,
                                    self._rho_-beta*rho_mod_)

    def _phase(self):
        if self._monitor:
            self._monitor_support()
        rho_hat_ = self._xp.fft.fftn(self._rho_)
        if self._monitor:
            self._monitor_Fourier(rho_hat_)
        phases_ = self._xp.angle(rho_hat_)
        rho_hat_mod_ = self._xp.where(
            self._amp_mask_,
            self._amplitudes_ * self._xp.exp(1j*phases_),
            rho_hat_)
        rho_mod_ = self._xp.fft.ifftn(rho_hat_mod_)
        support_star_ = self._support_
        if self._monitor:
            self._monitor_real(rho_mod_, support_star_)
        return rho_mod_, support_star_

    def _monitor_support(self):
        size = self._support_.sum()
        if self._is_gpu:  # cupy's norm return a 0d array
            size = size.get()[()]
        self._suppS_l.append(size)

    def _monitor_Fourier(self, rho_hat_):
        dist = (
            self._xp.linalg.norm(
                (self._xp.absolute(rho_hat_)
                 - self._amplitudes_)[self._amp_mask_])
            / self._n_values)
        if self._is_gpu:  # cupy's norm return a 0d array
            dist = dist.get()[()]
        self._distF_l.append(dist)

    def _monitor_real(self, rho_mod_, support_star_):
        dist = self._xp.linalg.norm(rho_mod_[~support_star_])
        if self._is_gpu:  # cupy's norm return a 0d array
            dist = dist.get()[()]
        self._distR_l.append(dist)

    def shrink_wrap(self, cutoff, sigma=1):
        if self._is_gpu:
            rho_ = cp.asnumpy(self._rho_)
        else:
            rho_ = self._rho_
        rho_abs_ = np.absolute(rho_)
        # By using 'wrap', we don't need to fftshift it back and forth
        rho_gauss_ = gaussian_filter(
            rho_abs_, mode='wrap', sigma=sigma, truncate=2)
        support_new_ = rho_gauss_ > rho_abs_.max() * cutoff
        self._support_[:] = self._xp.asarray(support_new_, dtype=np.bool_)

    def get_support_sizes(self):
        return np.array(self._suppS_l)

    def get_Fourier_errs(self):
        return np.array(self._distF_l)

    def get_real_errs(self):
        return np.array(self._distR_l)

    def get_support(self, ifftshifted=False):
        shift = np.fft.fftshift if not ifftshifted else lambda x: x
        if self._is_gpu:
            support_ = cp.asnumpy(self._support_)
        else:
            support_ = self._support_
        return shift(support_)

    def get_rho(self, ifftshifted=False):
        shift = np.fft.fftshift if not ifftshifted else lambda x: x
        if self._is_gpu:
            rho_ = cp.asnumpy(self._rho_)
        else:
            rho_ = self._rho_
        return shift(rho_)


class GPUNotAvailabeError(Exception):
    pass
