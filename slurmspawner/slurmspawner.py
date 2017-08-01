"""
*** SlurmSpawner ***
This is a custom spawner for Jupyterhub that will spawn jobs using the Slurm workload manager.
There are some things this script depends on:
    1. Jupyterhub is installed
    2. IPython is installed
    3. Of course, the most important part is that Slurm is installed and working on the system
"""

import signal
import errno
import pwd
import os
import time
import pipes
import shlex
from subprocess import Popen, call
import subprocess
from string import Template
from concurrent.futures import ThreadPoolExecutor

from tornado import gen

from traitlets import (
    Bool, Instance, Integer, Unicode
)

from jupyterhub.spawner import Spawner
from jupyterhub.spawner import set_user_setuid
from jupyterhub.utils import random_port

class SlurmException(Exception):
    pass

class SlurmSpawnerException(Exception):
    pass

def run_command(cmd):
    popen = subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    out = popen.communicate()
    if out[1] is not None:
        return out[1] # exit error?
    else:
        out = out[0].decode().strip()
        return out

class SlurmSpawner(Spawner):
    """A Spawner that just uses Popen to start local processes."""
    #### These lines are designed to be overridden by the admin in jupyterhub_config.py ###
     
    _executor = None
    @property
    def executor(self):
        """single global executor"""
        self.log.debug("Running executor...")
        cls = self.__class__
        if cls._executor is None:
            cls._executor = ThreadPoolExecutor(1)
        return cls._executor

    ip = Unicode("0.0.0.0", config=True, help="url of the server")
    slurm_job_id = Unicode() # will get populated after spawned
    pid = Integer(0)
    
    extra_launch_script = Unicode("/etc/jupyterhub/extra_launch_script", config=True, \
        help="bash script snippet that will be inserted into Slurm job script")
        
    # user-configurable options that can be changed in jupyterhub_config.py
    partition = Unicode("all", config=True, help="Slurm partition to launch the spawner on")
    mem = Integer(400, config=True, help="Slurm memory allocated for the spawner")
    time = Unicode("1-00:00:00", config=True, \
        help="Slurm max time to allow spawner to run (uses Slurm time format of dd-hhh:mm:ss)")
    ntasks = Integer(1, config=True, help="Slurm ntasks for spawner")
    cpus_per_task = Integer(1, config=True, help="Slurm cpus-per-task for spawner")
    nodes = Integer(1, config=True, help="Slurm number of nodes for spawner")
    qos = Unicode("normal", config=True, help="Slurm QOS to run spawner under")
    job_name = Unicode("spawner-jupyterhub-singleuser", config=True, help="Slurm job name for spawner")
    output = Unicode("/.ipython/jupyterhub-slurmspawner.log", config=True, \
        help="Slurm output file location -- this dir is appended to /home/$USER")
    run_with_sudo = Bool(False, config=True, help="run the sbatch command with sudo instead of just 'sbatch'")

    def make_preexec_fn(self, name):
        """make preexec fn"""
        return set_user_setuid(name)

    def load_state(self, state):
        """load slurm_job_id from state"""
        super(SlurmSpawner, self).load_state(state)
        self.slurm_job_id = state.get('slurm_job_id', '')
        self.slurm_port = state.get('slurm_port', '')

    def get_state(self):
        """add slurm_job_id to state"""
        state = super(SlurmSpawner, self).get_state()
        if self.slurm_job_id:
            state['slurm_job_id'] = self.slurm_job_id
        if self.slurm_port:
            state['slurm_port'] = self.slurm_port
        return state

    def clear_state(self):
        """clear slurm_job_id state"""
        super(SlurmSpawner, self).clear_state()
        self.slurm_job_id = ""
        self.slurm_port = ""

    def user_env(self, env):
        """get user environment"""
        env['USER'] = self.user.name
        env['HOME'] = pwd.getpwnam(self.user.name).pw_dir
        return env

    def get_env(self):
        env = super().get_env()
        return self.user_env(env)
    
    @gen.coroutine
    def stop_slurm_job(self):
        """Wrapper to call _stop_slurm_job() to be passed to ThreadPoolExecutor"""
        is_stopped = yield self.executor.submit(self._stop_slurm_job)
        return is_stopped
         
    def _stop_slurm_job(self):
        if self.slurm_job_id in (None, ""):
            self.log.warn("Slurm job id for user %s isn't defined!" % (self.user.name))
            return True

        cmd = 'scancel ' + self.slurm_job_id
        self.log.info("Cancelling slurm job %s for user %s" % (self.slurm_job_id, self.user.name))

        output = run_command(cmd)
        if output not in (None, ""):  # if job is valid, output of scancel should be nothing
            raise SlurmException("Failed to cancel job %s: slurm output: %s" % (self.slurm_job_id, output))

        job_state = self.check_slurm_job_state()
        self.log.debug("job state is %s" % job_state)
        if job_state in ("CANCELLED", "COMPLETED", "FAILED", "COMPLETING", ""):
            return True
        else:
            return False

    def check_slurm_job_state(self):
        self.log.debug("Checking slurm job %s" % self.slurm_job_id)
        if self.slurm_job_id in (None, ""):
            # job has been cancelled or failed, so don't even try the squeue command. This is because
            # squeue will return RUNNING if you submit something like `squeue -h -j -o %T` and there's
            # at least 1 job running
            return ""
        # check sacct to see if the job is still running
        cmd = 'squeue -h -j ' + self.slurm_job_id + ' -o %T'
        out = run_command(cmd)
        self.log.debug("squeue output: %s" % out)
        if "PENDING" in out:
            self.log.debug("job is PENDING. Checking reason...")
            reason = run_command('squeue -h -j ' + self.slurm_job_id + ' -O reason')
            # sometimes a job can fail and get requeued, which means it will just sit there and get stuck.
            # we need to combat that by saying that the job has actually failed, so that it will be killed
            if "failed" in reason:
                self.log.debug("'failed' was found in reason for pending. Marking job as FAILED")
                out = "FAILED"

        self.log.debug("Notebook server for user %s: Slurm jobid %s status: %s" % (self.user.name, self.slurm_job_id, out))
        return out
        
    def query_slurm_by_jobname(self, user, jobname):
        """
        uses slurm's squeue to see if there is currently a job called <jobname> running.
        If so, it returns the jobid
        """
        self.log.debug("Querying Slurm for user '%s' with jobname '%s'" % (user, jobname))
        cmd = 'squeue -h -u %s --name=%s -O jobid,comment,state,reason' % (user, jobname)
        self.log.debug("running command '%s'" % cmd)
        output = run_command(cmd).strip()
        output_list = output.split()
        self.log.debug("output list: %s" % output_list)
        if len(output_list) > 0:
            jobid = output_list[0]
            port = output_list[1]
            state = output_list[2]
            reason = output_list[3:]
        else:
            return ("", "", "", "")
        self.log.debug("Query found jobid '%s'" % (jobid))
        return (jobid, port, state, reason)

    @gen.coroutine
    def run_jupyterhub_singleuser(self, cmd, port, user):
        """ 
        Wrapper for calling run_jupyterhub_singleuser to be passed to ThreadPoolExecutor..
        """
        args = [cmd, port, user]
        server = yield self.executor.submit(self._run_jupyterhub_singleuser, *args)
        return server

    def _run_jupyterhub_singleuser(self, cmd, port, user):
        """
        Submits a slurm sbatch script to start jupyterhub-singleuser
        """
        # need to check if admin has supplied a Slurm template in /etc/jupyterhub
        if os.path.exists(str(self.extra_launch_script)):
            self.log.info("loading extra script snippet found at '%s' into slurm script" % self.extra_launch_script)
            f = open(str(self.extra_launch_script))
            sbatch = f.read()
        else:
            self.log.debug("No Slurm template found. Using defaults")
            sbatch = "# *** No user template found ***"

        full_cmd = cmd.split(';')
        export_cmd = full_cmd[0] 
        cmd = full_cmd[1]
        
        slurm_script = Template('''#!/bin/bash
#SBATCH --cpus-per-task=$cpus
#SBATCH --job-name=$job_name
#SBATCH --mem=$mem
#SBATCH --ntasks=$ntasks
#SBATCH --nodes=$nodes
#SBATCH --output=/home/$user/$output
#SBATCH --partition=$part
#SBATCH --qos=$qos
#SBATCH --time=$time
#SBATCH --workdir=/home/$user
#SBATCH --comment=$port
#SBATCH --open-mode=append
#SBATCH --uid=$user
#SBATCH --gid=$gid
#SBATCH --export=none
#SBATCH --get-user-env=L

##### USER-DEFINED TEMPLATE LOADED HERE #####
$sbatch
##### END USER-DEFINED TEMPLATE #############

echo "*** Spawning single-user server ***"
$export_cmd
$cmd

        ''')

        gid = pwd.getpwnam(user).pw_gid # get group id of user

        form = self.authenticator

        memory = self.mem
        cpus = self.cpus_per_task
        ntasks = self.ntasks
	# Name it stime so it doesnt conflict with the "time" object
        stime = self.time
        nodes = self.nodes

        if form.custom:
            self.log.debug('Changing SlurmSpawner options')
            if form.memory > 0:
                memory = form.memory
            if form.cpus > 0 and form.cpus <= 32:
                cpus = form.cpus
            if form.tasks > 0 and form.tasks <= 128:
                ntasks = form.tasks
            if form.time:
                # This is to assure that the string is in a format of INT-INT:INT:INT
		# We don't want to accept any random strings -- the int() will catch this
                tokens = form.time.split(':') 
                days = int(tokens[0].split('-')[0])
                hours = int(tokens[0].split('-')[1])
                minutes = int(tokens[1])
                seconds = int(tokens[2])
                if days <= 14:
                    stime = form.time
            if form.nodes > 0 and form.nodes <= 32:
                nodes = form.nodes
            form.custom = False

        slurm_script = slurm_script.substitute(dict(cpus=cpus,
                                                    job_name=self.job_name,
                                                    mem=memory,
                                                    ntasks=ntasks,
                                                    nodes=nodes,
                                                    output=self.output,
                                                    part=self.partition,
                                                    qos=self.qos,
                                                    time=stime,
                                                    sbatch=sbatch,
                                                    export_cmd=export_cmd,
                                                    cmd=cmd,
                                                    port=port,
                                                    user=user,
                                                    gid=gid))
        ########## HASH FILE CREATION (to make sure Slurm users can't abuse the QOS if it has high prio ##########
        # before we submit this job, we need to create a tmp file that will serve as a hash file that
        # slurm can check. If the hash value is wrong, it will know that this script did not submit the job
        # and will therefore not change any settings (this is because we are using a job_submit.lua script
        # to change the priority of the jupyterhub jobs
        uid = pwd.getpwnam(user).pw_uid # get userid of user
        if not os.path.exists("/tmp/jupyter"):
            os.mkdir("/tmp/jupyter")
        # check if file already exists
        try:
            file_name = "/tmp/jupyter/" + str(uid)
            hash_file = open(file_name, "w")
        except IOError:
            error = "Error opening hash file '%s' for writing" % file_name
            self.log.error(error)
            raise SlurmException(error)

        # convert port to hash number (just sum the digits)
        sum = 0
        for c in str(port):
            sum += int(c)
        hash = str(sum)
        hash_file.write(hash)
        hash_file.close()
        ###### END HASH FILE CREATION ##########

        if self.run_with_sudo:
            self.log.debug("Running sbatch with sudo privileges")
            cmd = "sudo sbatch"
        else:
            cmd = "sbatch"

        self.log.debug('Submitting *****{\n%s\n}*****' % slurm_script)
        popen = subprocess.Popen(cmd,
                                 shell=True, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE)
        output = popen.communicate(slurm_script.encode())[0].strip()  # e.g. something like "Submitted batch job 209"
        output = output.decode()  # convert bytes object to string

        if output == "" or len(output) == 0:
            error = "Slurm did not attempt to start the job! Check Slurm logs"
            self.log.error(error)
            raise SlurmException(error)

        self.log.debug("Stdout of trying to launch with sbatch: %s" % output)
        self.slurm_job_id = output.split(' ')[-1] # the job id should be the very last part of the string

        job_state = self.check_slurm_job_state()
        
        while True:
            self.log.info("job_state is %s" % job_state)
            if 'RUNNING' in job_state:
                break
            elif 'PENDING' in job_state:
                job_state = self.check_slurm_job_state()
                time.sleep(1)
            else:
                error = "Job %s failed to start!" % self.slurm_job_id
                self.log.error(error)
                raise SlurmException(error)

        node_ip, node_name  = self.get_slurm_job_info(self.slurm_job_id)

        if node_ip is None or node_name is None:
            error = "There appears to be no node info available for job %s" % self.slurm_job_id
            self.log.error(error)
            raise SlurmException(error)

        self.user.server.ip = node_ip 
        self.user.server.port = port
        self.log.info("Notebook server running on %s:%s (%s)" % (node_ip, port, node_name))
        return self.slurm_job_id

    def get_slurm_job_info(self, jobid):
        """returns tuple of ip address and name of node that is running the job"""
        self.log.debug("Getting slurm job info for job %s" % jobid)
        cmd = 'squeue -h -j ' + jobid + ' -o %N'
        self.log.debug("Running command: '%s'" % cmd)
        popen = subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        node_name = popen.communicate()[0].strip().decode() # convett bytes object to string
        # now get the ip address of the node name
        if node_name in (None, ""):
            return (None, None)
        cmd = 'host %s' % node_name
        self.log.debug("Running command: '%s'" % cmd)
        popen = subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        out = popen.communicate()[0].strip().decode()
        node_ip = out.split(' ')[-1] # the last portion of the output should be the ip address
        return (node_ip, node_name)

    @gen.coroutine
    def start(self):
        """Start the process"""
        self.log.debug("Running start() method...")
        # first check if the user has a spawner running somewhere on the server
        jobid, port, state, reason = self.query_slurm_by_jobname(self.user.name, self.job_name)

        if state == "COMPLETING":
            self.log.debug("job %s still completing. Resetting jobid and port to empty string so new job will start.")
            jobid = ""
            port = ""

        self.slurm_job_id = jobid
        self.user.server.port = port

        if "failed" in reason:  # e.g. "launch failed requeued held" means it'll never start. clear everything
            self.log.error("'failed' was found in squeue 'reason' output for job %s. Running scancel..." % self.slurm_job_id)
            self._stop_slurm_job()
            self.clear_state()
            self.user.spawn_pending = False
            self.db.commit()
            raise SlurmException("Slurm failed to launch job")

        if jobid != "" and port != "":
            self.log.debug("*** STARTED SERVER *** Server was found running with slurm jobid '%s' \
                            for user '%s' on port %s" % (jobid, self.user.name, port)) 
            node_ip, node_name = self.get_slurm_job_info(jobid)
            self.user.server.ip = node_ip
            return

        # if the above wasn't true, then it didn't find a state for the user
        self.user.server.port = random_port()

        cmd = []

        env = self.get_env()

        cmd.extend(self.cmd)
        cmd.extend(self.get_args())

        self.log.debug("Env: %s", str(self.get_env()))
        self.log.info("Spawning %s", ' '.join(cmd))
        for k in ["JPY_API_TOKEN"]:
            cmd.insert(0, 'export %s="%s";' % (k, env[k]))

        self.db.commit() # added this to test if there is a change in the way jupyterhub is working

        yield self.run_jupyterhub_singleuser(' '.join(cmd), self.user.server.port, self.user.name)

    @gen.coroutine
    def poll(self):
        """Poll the process"""
        self.log.debug("Polling job...")
        if self.slurm_job_id is not None:
            state = self.check_slurm_job_state()
            if "RUNNING" in state or "PENDING" in state:
                self.log.debug("Job found to be running/pending for %s on %s:%s" % (self.user.name, self.user.server.ip, self.user.server.port))
                return None
            else:
                if state == "":
                    self.log.debug("No job state found. Clearing state for %s", self.user.name)
                else:
                    self.log.debug("Job found to be %s. Clearing state for %s" % (state, self.user.name))
                    self._stop_slurm_job()
                    self.user.spawn_pending = False
                    self.db.commit()

                self.clear_state()
                return 127

        if not self.slurm_job_id:
            # no job id means it's not running
            self.log.debug("No job info to poll. Clearing state for %s" % self.user.name)
            self.clear_state()
            return 127

    @gen.coroutine
    def _signal(self, sig):
        """simple implementation of signal

        we can use it when we are using setuid (we are root)"""
        return True
    
    @gen.coroutine
    def stop(self, now=False):
        if not now:
            self.log.info("Stopping slurm job %s for user %s" % (self.slurm_job_id, self.user.name))
            is_stopped = yield self.stop_slurm_job()
            if not is_stopped:
                self.log.warn("Job %s didn't stop. Trying again..." % self.slurm_job_id)
                yield self.stop_slurm_job()
        
        self.clear_state()

