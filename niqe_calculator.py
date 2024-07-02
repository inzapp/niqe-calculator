import os
import cv2
import math
import scipy
import argparse
import numpy as np

from glob import glob
from tqdm import tqdm


class NIQECalculator:
    def __init__(self, patch_size='auto', params_path='params.mat'):
        self.patch_size = patch_size
        self.params_path = params_path

        self.gamma_range = np.arange(0.2, 10, 0.001)
        self.a = scipy.special.gamma(2.0 / self.gamma_range)
        self.a *= self.a
        self.b = scipy.special.gamma(1.0 / self.gamma_range)
        self.c = scipy.special.gamma(3.0 / self.gamma_range)
        self.prec_gammas = self.a / (self.b * self.c)

        self.params = None
        self.pop_mu = None
        self.pop_cov = None

    def aggd_features(self, patch):
        patch.shape = (len(patch.flat),)
        imdata2 = patch*patch
        left_data = imdata2[patch<0]
        right_data = imdata2[patch>=0]
        left_mean_sqrt = 0
        right_mean_sqrt = 0
        if len(left_data) > 0:
            left_mean_sqrt = np.sqrt(np.average(left_data))
        if len(right_data) > 0:
            right_mean_sqrt = np.sqrt(np.average(right_data))

        if right_mean_sqrt != 0:
          gamma_hat = left_mean_sqrt/right_mean_sqrt
        else:
          gamma_hat = np.inf
        # solve r-hat norm

        imdata2_mean = np.mean(imdata2)
        if imdata2_mean != 0:
          r_hat = (np.average(np.abs(patch))**2) / (np.average(imdata2))
        else:
          r_hat = np.inf
        rhat_norm = r_hat * (((math.pow(gamma_hat, 3) + 1)*(gamma_hat + 1)) / math.pow(math.pow(gamma_hat, 2) + 1, 2))

        # solve alpha by guessing values that minimize ro
        pos = np.argmin((self.prec_gammas - rhat_norm) ** 2);
        alpha = self.gamma_range[pos]

        gam1 = scipy.special.gamma(1.0/alpha)
        gam2 = scipy.special.gamma(2.0/alpha)
        gam3 = scipy.special.gamma(3.0/alpha)

        aggdratio = np.sqrt(gam1) / np.sqrt(gam3)
        bl = aggdratio * left_mean_sqrt
        br = aggdratio * right_mean_sqrt

        # mean parameter
        N = (br - bl) * (gam2 / gam1)
        return (alpha, N, bl, br, left_mean_sqrt, right_mean_sqrt)

    # def ggd_features(self, imdata):
    #     nr_gam = 1/self.prec_gammas
    #     sigma_sq = np.var(imdata)
    #     E = np.mean(np.abs(imdata))
    #     rho = sigma_sq/E**2
    #     pos = np.argmin(np.abs(nr_gam - rho));
    #     return self.gamma_range[pos], sigma_sq

    def paired_product(self, new_im):
        shift1 = np.roll(new_im.copy(), 1, axis=1)
        shift2 = np.roll(new_im.copy(), 1, axis=0)
        shift3 = np.roll(np.roll(new_im.copy(), 1, axis=0), 1, axis=1)
        shift4 = np.roll(np.roll(new_im.copy(), 1, axis=0), -1, axis=1)

        H_img = shift1 * new_im
        V_img = shift2 * new_im
        D1_img = shift3 * new_im
        D2_img = shift4 * new_im
        return (H_img, V_img, D1_img, D2_img)

    def gen_gauss_window(self, lw, sigma):
        sd = np.float32(sigma)
        lw = int(lw)
        weights = [0.0] * (2 * lw + 1)
        weights[lw] = 1.0
        sum = 1.0
        sd *= sd
        for ii in range(1, lw + 1):
            tmp = np.exp(-0.5 * np.float32(ii * ii) / sd)
            weights[lw + ii] = tmp
            weights[lw - ii] = tmp
            sum += 2.0 * tmp
        for ii in range(2 * lw + 1):
            weights[ii] /= sum
        return weights

    def compute_image_mscn_transform(self, img, C=1, avg_window=None, extend_mode='constant'):
        if avg_window is None:
            avg_window = self.gen_gauss_window(3, 7.0/6.0)
        h, w = img.shape[:2]
        mu_image = np.zeros(img.shape, dtype=np.float32)
        var_image = np.zeros(img.shape, dtype=np.float32)
        img = np.array(img).astype(np.float32)
        scipy.ndimage.correlate1d(img, avg_window, 0, mu_image, mode=extend_mode)
        scipy.ndimage.correlate1d(mu_image, avg_window, 1, mu_image, mode=extend_mode)
        scipy.ndimage.correlate1d(img**2, avg_window, 0, var_image, mode=extend_mode)
        scipy.ndimage.correlate1d(var_image, avg_window, 1, var_image, mode=extend_mode)
        var_image = np.sqrt(np.abs(var_image - mu_image**2))
        return (img - mu_image) / (var_image + C), var_image, mu_image

    def _niqe_extract_subband_feats(self, mscncoefs):
        alpha_m, N, bl, br, lsq, rsq = self.aggd_features(mscncoefs.copy())
        pps1, pps2, pps3, pps4 = self.paired_product(mscncoefs)
        alpha1, N1, bl1, br1, lsq1, rsq1 = self.aggd_features(pps1)
        alpha2, N2, bl2, br2, lsq2, rsq2 = self.aggd_features(pps2)
        alpha3, N3, bl3, br3, lsq3, rsq3 = self.aggd_features(pps3)
        alpha4, N4, bl4, br4, lsq4, rsq4 = self.aggd_features(pps4)
        return np.array([alpha_m, (bl+br)/2.0,
                alpha1, N1, bl1, br1,  # (V)
                alpha2, N2, bl2, br2,  # (H)
                alpha3, N3, bl3, bl3,  # (D1)
                alpha4, N4, bl4, bl4,  # (D2)
        ])

    def extract_on_patches(self, img, patch_size):
        h, w = img.shape[:2]
        patch_size = int(patch_size)
        patches = []
        for j in range(0, h-patch_size+1, patch_size):
            for i in range(0, w-patch_size+1, patch_size):
                patch = img[j:j+patch_size, i:i+patch_size]
                patches.append(patch)
        patches = np.array(patches)
        patch_features = []
        for p in patches:
            patch_features.append(self._niqe_extract_subband_feats(p))
        patch_features = np.array(patch_features)
        return patch_features

    def _get_patches_generic(self, img, patch_size, is_train, stride):
        h, w = img.shape[:2]
        if h < patch_size or w < patch_size:
            print('input image is too small')
            exit(0)

        # ensure that the patch divides evenly into img
        hoffset = (h % patch_size)
        woffset = (w % patch_size)

        if hoffset > 0: 
            img = img[:-hoffset, :]
        if woffset > 0:
            img = img[:, :-woffset]

        img = np.asarray(img).astype(np.float32)
        img2 = cv2.resize(img, (0, 0), fx=0.5, fy=0.5, interpolation=cv2.INTER_CUBIC)

        mscn1, var, mu = self.compute_image_mscn_transform(img)
        mscn1 = mscn1.astype(np.float32)

        mscn2, _, _ = self.compute_image_mscn_transform(img2)
        mscn2 = mscn2.astype(np.float32)

        feats_lvl1 = self.extract_on_patches(mscn1, patch_size)
        feats_lvl2 = self.extract_on_patches(mscn2, patch_size/2)

        feats = np.hstack((feats_lvl1, feats_lvl2))
        return feats

    def get_patches_train_features(self, img, patch_size, stride=8):
        return self._get_patches_generic(img, patch_size, 1, stride)

    def get_patches_test_features(self, img, patch_size, stride=8):
        return self._get_patches_generic(img, patch_size, 0, stride)

    def get_auto_patch_size(self, img_w, img_h):
        assert img_w >= 18 and img_h >= 18, 'image size must be over than 18x18'
        patch_sizes = [96, 64, 48, 32, 16, 8]
        for patch_size in patch_sizes:
            if img_w > (patch_size * 2 + 1) and img_h > (patch_size * 2 + 1):
                return patch_size
        return -1

    def niqe(self, img):
        if self.params is None:
            self.params = scipy.io.loadmat(self.params_path)
            self.pop_mu = np.ravel(self.params['pop_mu'])
            self.pop_cov = self.params['pop_cov']
        if len(img.shape) == 3:
            img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

        M, N = img.shape[:2]
        patch_size = self.get_auto_patch_size(N, M) if self.patch_size == 'auto' else self.patch_size
        assert M > (patch_size * 2 + 1), f'niqe called with small frame size, requires > {self.patch_size * 2}x{self.patch_size * 2} resolution video using current training parameters'
        assert N > (patch_size * 2 + 1), f'niqe called with small frame size, requires > {self.patch_size * 2}x{self.patch_size * 2} resolution video using current training parameters'

        feats = self.get_patches_test_features(img, patch_size)
        sample_mu = np.mean(feats, axis=0)
        sample_cov = np.cov(feats.T)

        X = sample_mu - self.pop_mu
        covmat = ((self.pop_cov + sample_cov) / 2.0)
        pinvmat = scipy.linalg.pinv(covmat)
        niqe_score = np.sqrt(np.dot(np.dot(X, pinvmat), X))
        return niqe_score


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--path', type=str, default='.', help='image path for calculate niqe score')
    parser.add_argument('--r', action='store_true', help='find images recursively using given path')
    args = parser.parse_args()
    niqe_calculator = NIQECalculator()
    cnt = 0
    niqe_sum = 0.0
    paths = glob(f'{args.path}/**/*.jpg' if args.r else f'{args.path}/*.jpg', recursive=args.r)
    for path in tqdm(paths):
        img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
        niqe_sum += niqe_calculator.niqe(img)
        cnt += 1
    if cnt > 0:
        niqe_avg = niqe_sum / float(cnt)
        print(f'NIQE : {niqe_avg:.2f}')
    else:
        print(f'no images found')

