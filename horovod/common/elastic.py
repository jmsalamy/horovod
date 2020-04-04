# Copyright 2020 Uber Technologies, Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

from __future__ import absolute_import

import functools

from six.moves import queue

from horovod.common.exceptions import HorovodInternalError, HostsUpdatedException
from horovod.run.elastic.worker import WorkerNotificationManager


notification_manager = WorkerNotificationManager()


class State(object):
    """State representation used for tracking in memory state across workers."""
    def __init__(self):
        self._host_messages = queue.Queue()
        self._last_updated_timestamp = 0
        self._reset_callbacks = []

    def register_reset_callbacks(self, callbacks):
        """Register callbacks that will be invoked following a reset event (worker added or removed).

        For example, a common use of a reset callback would be to update the learning rate scale with the
        new number of workers.

        Args:
            callbacks: list of functions to execute.
        """
        self._reset_callbacks.extend(callbacks)

    def on_reset(self):
        for callback in self._reset_callbacks:
            callback()

    def on_hosts_updated(self, timestamp):
        self._host_messages.put(timestamp)

    def commit(self):
        """Commits all modifications to state tracked by this object to host memory.

        This call will also check for any changes to known hosts, and raise a `WorkersAvailableException`
        if any were detected.

        Because commits are a heavy operation involving data copy (potentially from GPU to host), it is
        recommended to consider committing less frequently than once per batch. This allows users to tradeoff
        between per-batch execution time, and lost training steps in the event of a worker failure.
        """
        self.save()
        self._update_known_hosts()

    def save(self):
        """Saves state to host memory."""
        raise NotImplementedError()

    def restore(self):
        """Restores the last committed state, undoing any uncommitted modifications."""
        raise NotImplementedError()

    def sync(self):
        """Synchronize state across workers."""
        raise NotImplementedError()

    def _update_known_hosts(self):
        # Iterate through the update messages sent from the server. If the update timestamp
        # is greater than the last update timestamp, then trigger a HostsUpdatedException.
        updated = False
        while not self._host_messages.empty():
            timestamp = self._host_messages.get()
            if timestamp > self._last_updated_timestamp:
                self._last_updated_timestamp = timestamp
                updated = True

        # In order to ensure all workers raise the exception at the same time, we need to sync
        # the updated state across all the workers.
        updated = self._sync_host_updates(updated)

        # At this point, updated state is globally consistent across all ranks.
        if updated:
            raise HostsUpdatedException()

    def _sync_host_updates(self, updated):
        raise NotImplementedError()


class ObjectState(State):
    """State for simple Python objects.

    Every object is specified as a keyword argument, and will be assigned as an attribute.

    Args:
        bcast_object: Horovod broadcast object function used to sync state dictionary.
        kwargs: Properties to sync, will be exposed as attributes of the object.
    """
    def __init__(self, bcast_object, **kwargs):
        self._bcast_object = bcast_object
        self._saved_state = kwargs
        self._set_attrs()
        super(ObjectState, self).__init__()

    def save(self):
        new_state = {}
        for attr in self._saved_state.keys():
            new_state[attr] = getattr(self, attr)
        self._saved_state = new_state

    def restore(self):
        self._set_attrs()

    def sync(self):
        if self._saved_state:
            self._saved_state = self._bcast_object(self._saved_state)
            self._set_attrs()

    def _set_attrs(self):
        for attr, value in self._saved_state.items():
            setattr(self, attr, value)

    def _sync_host_updates(self, updated):
        return self._bcast_object(updated)


def run_fn(func, reset):
    @functools.wraps(func)
    def wrapper(state, *args, **kwargs):
        notification_manager.init()
        notification_manager.register_listener(state)

        try:
            reset_required = False
            while True:
                if reset_required:
                    reset()
                    state.on_reset()

                state.sync()
                try:
                    return func(state, *args, **kwargs)
                except HorovodInternalError:
                    state.restore()
                except HostsUpdatedException:
                    pass
                reset_required = True
        finally:
            notification_manager.remove_listener(state)
    return wrapper