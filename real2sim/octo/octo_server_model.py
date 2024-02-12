from collections import defaultdict
import numpy as np
import os, time
import jax, cv2
import tensorflow as tf
import matplotlib.pyplot as plt
import json, requests, urllib

from numpy.lib.format import dtype_to_descr, descr_to_dtype
from base64 import b64decode, b64encode
from sapien.core import Pose
from transforms3d.euler import euler2axangle


def default(obj):
    if isinstance(obj, (np.ndarray, np.generic)):
        return {
            '__numpy__': b64encode(obj.data if obj.flags.c_contiguous else obj.tobytes()).decode('ascii'),
            'dtype': dtype_to_descr(obj.dtype),
            'shape': obj.shape
        }
    raise TypeError(f'Object of type {type(obj)} is not JSON serializable')


def object_hook(dct):
    if '__numpy__' in dct:
        np_obj = np.frombuffer(b64decode(dct['__numpy__']), descr_to_dtype(dct['dtype']))
        shape = dct['shape']
        return np_obj.reshape(shape) if shape else np_obj[0]  # Scalar test
    return dct


_dumps = json.dumps
_loads = json.loads
_dump = json.dump
_load = json.load


def dumps(*args, **kwargs):
    kwargs.setdefault('default', default)
    return _dumps(*args, **kwargs)


def loads(*args, **kwargs):
    kwargs.setdefault('object_hook', object_hook)
    return _loads(*args, **kwargs)


def dump(*args, **kwargs):
    'test'
    kwargs.setdefault('default', default)
    return _dump(*args, **kwargs)


def load(*args, **kwargs):
    kwargs.setdefault('object_hook', object_hook)
    return _load(*args, **kwargs)


def patch():
    """Monkey patches the json module in order to support serialization/deserialization of Numpy arrays and scalars."""
    json.dumps = dumps
    json.loads = loads
    json.dump = dump
    json.load = load

patch()



class OctoServerInference:
    def __init__(
        self,
        model_type="octo-base",
        policy_setup='widowx_bridge',
        image_size=256,
        action_scale=1.0,
    ):
        if policy_setup == 'widowx_bridge':
            self.sticky_gripper_num_repeat = 1
            raise NotImplementedError("Action normalization might be wrong, TODO")
        elif policy_setup == 'google_robot':
            self.sticky_gripper_num_repeat = 15
        else:
            raise NotImplementedError(f"Policy setup {policy_setup} not supported for octo models.")
        self.policy_setup = policy_setup
        
        self.sticky_action_is_on = False
        self.gripper_action_repeat = 0
        self.sticky_gripper_action = 0.0
        self.previous_gripper_action = None
            
        self.image_size = image_size
        self.action_scale = action_scale
        self.task = None
        self.time_step = 0

    def _resize_image(self, image):
        image = tf.image.resize(
            image, size=(self.image_size, self.image_size), method="lanczos3", antialias=True
        )
        image = tf.cast(tf.clip_by_value(tf.round(image), 0, 255), tf.uint8).numpy()
        return image
        
    def reset(self, task_description):
        self.task = task_description
        self.time_step = 0
        self.sticky_action_is_on = False
        self.gripper_action_repeat = 0
        self.sticky_gripper_action = 0.0
        self.previous_gripper_action = None
        _ = requests.post(urllib.parse.urljoin("http://ari.bair.berkeley.edu:8000", "reset"),)
        time.sleep(1.0)

    @staticmethod
    def _get_fake_pay_load(image_primary, text, modality='l'):
        payload = {
            "observation": {
                "image_primary": image_primary,
            },
            "text": text,
            "modality": modality,
            "ensemble": True
        }
        fake_pay_load = {'use_this': dumps(payload)}
        return fake_pay_load

    def _query_for_action(self, image_primary, text, goal, modality='l'):
        del goal
        # _ = requests.post(urllib.parse.urljoin("http://ari.bair.berkeley.edu:8000", "reset"),)
        fake_pay_load = self._get_fake_pay_load(image_primary, text, modality)
        reply = requests.post(urllib.parse.urljoin("http://ari.bair.berkeley.edu:8000", "query"),
                        json=fake_pay_load, timeout=100).json()
        # print(reply)
        return loads(reply)
    
    def step(self, image, *args, **kwargs):
        """
        Input:
            image: np.ndarray of shape (H, W, 3), uint8
        Output:
            raw_action: dict; raw policy action output
            action: dict; processed action to be sent to the maniskill2 environment, with the following keys:
                - 'world_vector': np.ndarray of shape (3,), xyz translation of robot end-effector
                - 'rot_axangle': np.ndarray of shape (3,), axis-angle representation of end-effector rotation
                - 'gripper': np.ndarray of shape (1,), gripper action
                - 'terminate_episode': np.ndarray of shape (1,), 1 if episode should be terminated, 0 otherwise
        """
        assert image.dtype == np.uint8
        image = self._resize_image(image)
        
        raw_action = self._query_for_action(image, self.task, goal=None)
        raw_action = {
            "world_vector": np.array(raw_action[:3]),
            "rotation_delta": np.array(raw_action[3:6]),
            "open_gripper": np.array(raw_action[-1:]), # range [0, 1]; 1 = open; 0 = close
        }
        
        # process raw_action to obtain the action to be sent to the maniskill2 environment
        action = {}
        action['world_vector'] = raw_action['world_vector'] * self.action_scale
        action_rotation_delta = np.asarray(raw_action['rotation_delta'], dtype=np.float64)
        roll, pitch, yaw = action_rotation_delta
        action_rotation_ax, action_rotation_angle = euler2axangle(roll, pitch, yaw)
        action_rotation_axangle = action_rotation_ax * action_rotation_angle
        action['rot_axangle'] = action_rotation_axangle * self.action_scale
        
        if self.policy_setup == 'google_robot':
            current_gripper_action = raw_action['open_gripper']
            if self.previous_gripper_action is None:
                relative_gripper_action = np.array([0])
            else:
                relative_gripper_action = self.previous_gripper_action - current_gripper_action # google robot 1 = close; -1 = open
            self.previous_gripper_action = current_gripper_action
        
            if np.abs(relative_gripper_action) > 0.5 and self.sticky_action_is_on is False:
                self.sticky_action_is_on = True
                self.sticky_gripper_action = relative_gripper_action

            if self.sticky_action_is_on:
                self.gripper_action_repeat += 1
                relative_gripper_action = self.sticky_gripper_action

            if self.gripper_action_repeat == self.sticky_gripper_num_repeat:
                self.sticky_action_is_on = False
                self.gripper_action_repeat = 0
                self.sticky_gripper_action = 0.0
        
            action['gripper'] = relative_gripper_action
        elif self.policy_setup == 'widowx_bridge':
            action['gripper'] = 2.0 * (raw_action['open_gripper'] > 0.5) - 1.0 # binarize gripper action to 1 (open) and -1 (close)
        
        action['terminate_episode'] = np.array([0.0])
        
        self.time_step += 1
        
        return raw_action, action
    
    def visualize_epoch(self, predicted_raw_actions, images, save_path):
        images = [self._resize_image(image) for image in images]
        ACTION_DIM_LABELS = ['x', 'y', 'z', 'yaw', 'pitch', 'roll', 'grasp']

        img_strip = np.concatenate(np.array(images[::3]), axis=1)

        # set up plt figure
        figure_layout = [
            ['image'] * len(ACTION_DIM_LABELS),
            ACTION_DIM_LABELS
        ]
        plt.rcParams.update({'font.size': 12})
        fig, axs = plt.subplot_mosaic(figure_layout)
        fig.set_size_inches([45, 10])

        # plot actions
        pred_actions = np.array([np.concatenate([a['world_vector'], a['rotation_delta'], a['open_gripper']], axis=-1) for a in predicted_raw_actions])
        for action_dim, action_label in enumerate(ACTION_DIM_LABELS):
            # actions have batch, horizon, dim, in this example we just take the first action for simplicity
            axs[action_label].plot(pred_actions[:, action_dim], label='predicted action')
            axs[action_label].set_title(action_label)
            axs[action_label].set_xlabel('Time in one episode')

        axs['image'].imshow(img_strip)
        axs['image'].set_xlabel('Time in one episode (subsampled)')
        plt.legend()
        plt.savefig(save_path)
        
