import datetime
import itertools
import logging
import md5
import random
import time
import signal
import sys

from lockfile import LockTimeout

from hlsclient import helpers
from hlsclient.balancer import Balancer
from hlsclient.combine import get_actions
from hlsclient.consumer import consume_from_balancer
from hlsclient.discover import discover_playlists, get_servers
from hlsclient.lock import ExpiringLinkLockFile

MAX_TTL_IN_SECONDS = 600

class PlaylistWorker(object):
    def __init__(self, playlist, is_variant=False):
        self.playlist = playlist
        self.is_variant = is_variant
        self.config = helpers.load_config()
        self.setup_lock()

    def setup(self):
        helpers.setup_logging(self.config, "worker for {}".format(self.playlist))
        logging.debug('HLS CLIENT Started for {}'.format(self.playlist))

        self.destination = self.config.get('hlsclient', 'destination')
        self.encrypt = self.config.getboolean('hlsclient', 'encrypt')
        not_modified_tolerance = self.config.getint('hlsclient', 'not_modified_tolerance')
        self.balancer = Balancer(not_modified_tolerance)

        ttl = datetime.timedelta(seconds=random.randint(1, MAX_TTL_IN_SECONDS))
        self.death_time = datetime.datetime.now() + ttl

    def run_forever(self):
        self.setup()
        signal.signal(signal.SIGTERM, self.interrupted)
        while self.should_run():
            try:
                self.run_if_locking()
            except LockTimeout:
                logging.debug("Unable to acquire lock")
            except Exception as e:
                logging.exception('An unknown error happened')
            except KeyboardInterrupt:
                logging.debug('Quitting...')
                break
            time.sleep(0.1)
        self.stop()

    def run(self):
        playlists = discover_playlists(self.config)
        worker_playlists = self.filter_playlists_for_worker(playlists)
        if not worker_playlists:
            logging.warning("Playlist is not available anymore")
            self.stop()

        paths = get_servers(worker_playlists)
        self.balancer.update(paths)
        consume_from_balancer(self.balancer,
                              worker_playlists,
                              self.destination,
                              self.encrypt)

    def filter_playlists_for_worker(self, playlists):
        if self.is_variant:
            combine_actions = get_actions(playlists, "combine")
            my_combine_actions = [action for action in combine_actions if action['output'] == self.playlist]
            my_inputs = [action['input'] for action in my_combine_actions]
            streams = itertools.chain(*my_inputs)
            streams = [s for s in streams if s in playlists['streams']] # transcoded playlists are ignored
        else:
            streams = [self.playlist]
        return {"streams": {stream: playlists['streams'][stream] for stream in streams}}

    def should_run(self):
        should_live = datetime.datetime.now() < self.death_time
        if not should_live:
            logging.info("Worker {} should die now!".format(self.worker_id()))
        return should_live

    def interrupted(self, *args):
        logging.info('Interrupted. Releasing lock.')
        self.stop()

    def setup_lock(self):
        lock_path = self.lock_path()
        self.lock_timeout = self.config.getint('lock', 'timeout')
        self.lock_expiration = self.config.getint('lock', 'expiration')
        self.lock = ExpiringLinkLockFile(lock_path)

    def lock_path(self):
        return '{0}.{1}'.format(self.config.get('lock', 'path'), self.worker_id())

    def worker_id(self):
        return md5.md5(self.playlist).hexdigest()

    def run_if_locking(self):
        if self.can_run():
            self.lock.update_lock()
            self.run()

    def can_run(self):
        if self.other_is_running():
            logging.warning("Someone else acquired the lock")
            self.stop()
        elif not self.lock.is_locked():
            self.lock.acquire(timeout=self.lock_timeout)
        return self.lock.i_am_locking()

    def other_is_running(self):
        other = self.lock.is_locked() and not self.lock.i_am_locking()
        if other and self.lock.expired(tolerance=self.lock_expiration):
            logging.warning("Lock expired. Breaking it")
            self.lock.break_lock()
            return False
        return other

    def stop(self):
        try:
            self.lock.release_if_locking()
        finally:
            sys.exit(0)