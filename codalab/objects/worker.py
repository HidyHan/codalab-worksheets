'''
Worker is a class that executes bundles that need to be run.

It requires direct access to the bundle database and data store, and it
provides a few methods once it is initialized:
  update_created_bundles: update bundles that are blocking on others.
  update_ready_bundles: run a single bundle in the ready state.
'''
import contextlib
import datetime
import random
import subprocess
import sys
import time
import tempfile
import traceback

from codalab.common import (
  precondition,
  State,
  UsageError,
)
from codalab.lib import path_util


class Worker(object):
  def __init__(self, bundle_store, model):
    self.bundle_store = bundle_store
    self.model = model
    self.profiling_depth = 0

  def pretty_print(self, message):
    time_str = datetime.datetime.utcnow().isoformat()[:19].replace('T', ' ')
    print '%s: %s%s' % (time_str, '  '*self.profiling_depth, message)

  @contextlib.contextmanager
  def profile(self, message):
    self.pretty_print(message)
    self.profiling_depth += 1
    start_time = time.time()
    yield
    elapsed_time = time.time() - start_time
    self.profiling_depth -= 1
    self.pretty_print('Done! Took %0.2fs.' % (elapsed_time,))

  def update_bundle_states(self, bundles, new_state):
    '''
    Update a list of bundles all in one state to all be in the new_state.
    Return True if all updates succeed.
    '''
    if bundles:
      message = 'Setting %s bundles to %s...' % (
        len(bundles),
        new_state.upper(),
      )
      with self.profile(message):
        states = set(bundle.state for bundle in bundles)
        precondition(len(states) == 1, 'Got multiple states: %s' % (states,))
        success = self.model.batch_update_bundles(
          bundles=bundles,
          update={'state': new_state},
          condition={'state': bundles[0].state},
        )
        if not success:
          self.pretty_print('WARNING: update failed!')
        return success
    return True

  def update_created_bundles(self):
    '''
    Scan through CREATED bundles check their dependencies' statuses.
    If any parent is FAILED, move them to FAILED.
    If all parents are READY, move them to STAGED.
    '''
    print '-- Updating CREATED bundles! --'
    with self.profile('Getting CREATED bundles...'):
      bundles = self.model.batch_get_bundles(state=State.CREATED)
      self.pretty_print('Got %s bundles.' % (len(bundles),))
    parent_uuids = set(
      dep.parent_uuid for bundle in bundles for dep in bundle.dependencies
    )
    with self.profile('Getting parents...'):
      parents = self.model.batch_get_bundles(uuid=parent_uuids)
      self.pretty_print('Got %s bundles.' % (len(parents),))
    all_parent_states = {parent.uuid: parent.state for parent in parents}
    all_parent_uuids = set(all_parent_states)
    bundles_to_fail = []
    bundles_to_stage = []
    for bundle in bundles:
      parent_uuids = set(dep.parent_uuid for dep in bundle.dependencies)
      missing_uuids = parent_uuids - all_parent_uuids
      if missing_uuids:
        bundles_to_fail.append(
          (bundle, 'Missing parent bundles: %s' % (', '.join(missing_uuids),)))
      parent_states = {uuid: all_parent_states[uuid] for uuid in parent_uuids}
      failed_uuids = [
        uuid for (uuid, state) in parent_states.iteritems()
        if state == State.FAILED
      ]
      if failed_uuids:
        bundles_to_fail.append(
          (bundle, 'Parent bundles failed: %s' % (', '.join(failed_uuids),)))
      elif all(state == State.READY for state in parent_states.itervalues()):
        bundles_to_stage.append(bundle)
    with self.profile('Failing %s bundles...' % (len(bundles_to_fail),)):
      for (bundle, failure_message) in bundles_to_fail:
        metadata_update = {'failure_message': failure_message}
        update = {'state': State.FAILED, 'metadata': metadata_update}
        self.model.update_bundle(bundle, update)
    self.update_bundle_states(bundles_to_stage, State.STAGED)
    num_blocking = len(bundles) - len(bundles_to_fail) - len(bundles_to_stage)
    self.pretty_print('%s bundles are still blocking.' % (num_blocking,))
    print ''

  def update_staged_bundles(self):
    '''
    If there are any STAGED bundles, pick one and try to lock it.
    If we get a lock, move the locked bundle to RUNNING and then run it.
    '''
    print '-- Updating STAGED bundles! --'
    with self.profile('Getting STAGED bundles...'):
      bundles = self.model.batch_get_bundles(state=State.STAGED)
      self.pretty_print('Got %s bundles.' % (len(bundles),))
    random.shuffle(bundles)
    for bundle in bundles:
      if self.update_bundle_states([bundle], State.RUNNING):
        self.run_bundle(bundle)
        break
    else:
      self.pretty_print('Failed to lock a bundle!')
    print ''

  def run_bundle(self, bundle):
    '''
    Run the given bundle and then update its state to be either READY or FAILED.
    If the bundle is now READY, its data_hash should be set.
    '''
    # Check that we're running a bundle in the RUNNING state.
    state_message = 'Unexpected bundle state: %s' % (bundle.state,)
    precondition(bundle.state == State.RUNNING, state_message)
    data_hash_message = 'Unexpected bundle data_hash: %s' % (bundle.data_hash,)
    precondition(bundle.data_hash is None, data_hash_message)
    # Compute a dict mapping parent_uuid -> parent for each dep of this bundle.
    parent_uuids = set(dep.parent_uuid for dep in bundle.dependencies)
    parents = self.model.batch_get_bundles(uuid=parent_uuids)
    parent_dict = {parent.uuid: parent for parent in parents}
    # Create a scratch directory to run the bundle in.
    with self.profile('Creating temp directory...'):
      temp_dir = tempfile.mkdtemp()
    # Run the bundle. Mark it READY if it is successful and FAILED otherwise.
    with self.profile('Running bundle...'):
      print '\n-- Run started! --\nRunning %s.' % (bundle,)
      try:
        (data_hash, metadata) = bundle.run(
          self.bundle_store, parent_dict, temp_dir)
        self.finalize_run(bundle, State.READY, data_hash, metadata)
        print 'Got data hash: %s\n-- Success! --\n' % (data_hash,)
      except Exception:
        # TODO(skishore): Add metadata updates: time / CPU of run.
        (type, error, tb) = sys.exc_info()
        failure_message = '%s: %s' % (error.__class__.__name__, error)
        if not isinstance(error, UsageError):
          failure_message = 'Traceback:\n%s\n%s' % (
            ''.join(traceback.format_tb(tb))[:-1],
            failure_message,
          )
        with self.profile('Uploading failed bundle...'):
          (data_hash, metadata) = self.upload_failed_bundle(error, temp_dir)
        metadata.update({'failure_message': failure_message})
        self.finalize_run(bundle, State.FAILED, data_hash, metadata)
        print '-- FAILED! --\n%s\n' % (failure_message,)
    # Clean up after the run.
    with self.profile('Cleaning up temp directory...'):
      path_util.remove(temp_dir)

  def upload_failed_bundle(self, error, temp_dir):
    '''
    Try to upload some data for a failed bundle run. Return a (data_hash, metadata)
    pair if this fallback upload was successful, or (None, {}) if not.
    '''
    if isinstance(error, subprocess.CalledProcessError):
      # The exception happened in the bundle's binary, not in our Python code.
      # Right now, this is the only case in which we upload the failed bundle.
      path_util.remove_symlinks(temp_dir)
      try:
        return self.bundle_store.upload(temp_dir)
      except Exception:
        pass
    return (None, {})

  def finalize_run(self, bundle, state, data_hash, metadata=None):
    '''
    Update a bundle to the new state and data hash at the end of a run.
    '''
    update = {'state': state, 'data_hash': data_hash}
    if metadata:
      update['metadata'] = metadata
    with self.profile('Setting 1 bundle to %s...' % (state.upper(),)):
      self.model.update_bundle(bundle, update)
