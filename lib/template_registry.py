import weakref
import binascii
import util
import StringIO
import settings
from twisted.internet import defer
from lib.exceptions import SubmitException

import lib.logger
log = lib.logger.get_logger('template_registry')
log.debug("Got to Template Registry")
from mining.interfaces import Interfaces
from extranonce_counter import ExtranonceCounter
import lib.settings as settings

class JobIdGenerator(object):
    '''Generate pseudo-unique job_id. It does not need to be absolutely unique,
    because pool sends "clean_jobs" flag to clients and they should drop all previous jobs.'''
    counter = 0
    
    @classmethod
    def get_new_id(cls):
        cls.counter += 1
        if cls.counter % 0xffff == 0:
            cls.counter = 1
        return "%x" % cls.counter
                
class TemplateRegistry(object):
    '''Implements the main logic of the pool. Keep track
    on valid block templates, provide internal interface for stratum
    service and implements block validation and submits.'''
    
    def __init__(self, block_template_class, coinbaser, bitcoin_rpc, instance_id,
                 on_template_callback, on_block_callback):
        self.prevhashes = {}
        self.jobs = weakref.WeakValueDictionary()
        
        self.extranonce_counter = ExtranonceCounter(instance_id)
        self.extranonce2_size = block_template_class.coinbase_transaction_class.extranonce_size \
                - self.extranonce_counter.get_size()
        log.debug("Got to Template Registry")
        self.coinbaser = coinbaser
        self.block_template_class = block_template_class
        self.bitcoin_rpc = bitcoin_rpc
        self.on_block_callback = on_block_callback
        self.on_template_callback = on_template_callback
        
        self.last_block = None
        self.update_in_progress = False
        self.last_update = None
        
        # Create first block template on startup
        self.update_block()
        
    def get_new_extranonce1(self):
        '''Generates unique extranonce1 (e.g. for newly
        subscribed connection.'''
        log.debug("Getting Unique Extranonce")
        return self.extranonce_counter.get_new_bin()
    
    def get_last_broadcast_args(self):
        '''Returns arguments for mining.notify
        from last known template.'''
        log.debug("Getting Last Template")
        return self.last_block.broadcast_args
        
    def add_template(self, block,block_height):
        '''Adds new template to the registry.
        It also clean up templates which should
        not be used anymore.'''
        
        prevhash = block.prevhash_hex

        if prevhash in self.prevhashes.keys():
            new_block = False
        else:
            new_block = True
            self.prevhashes[prevhash] = []
               
        # Blocks sorted by prevhash, so it's easy to drop
        # them on blockchain update
        self.prevhashes[prevhash].append(block)
        
        # Weak reference for fast lookup using job_id
        self.jobs[block.job_id] = block
        
        # Use this template for every new request
        self.last_block = block
        
        # Drop templates of obsolete blocks
        for ph in self.prevhashes.keys():
            if ph != prevhash:
                del self.prevhashes[ph]

        if new_block:
            # Tell the system about new block
            # It is mostly important for share manager
            self.on_block_callback(prevhash, block_height)

        # Everything is ready, let's broadcast jobs!
        self.on_template_callback(new_block)

    def update_block(self):
        '''Registry calls the getblocktemplate() RPC
        and build new block template.'''

        if self.update_in_progress:
            # Block has been already detected
            return
        
        self.update_in_progress = True
        self.last_update = Interfaces.timestamper.time()

        d = self.bitcoin_rpc.getblocktemplate()
        d.addCallback(self._update_block)
        d.addErrback(self._update_block_failed)

    def _update_block_failed(self, failure):
        log.error(str(failure))
        self.update_in_progress = False
        
    def _update_block(self, data):
        start = Interfaces.timestamper.time()
                
        template = self.block_template_class(Interfaces.timestamper, self.coinbaser, JobIdGenerator.get_new_id())
        log.info(template.fill_from_rpc(data))
        self.add_template(template,data['height'])

        log.info("Update finished, %.03f sec, %d txes" % \
                    (Interfaces.timestamper.time() - start, len(template.vtx)))
        
        self.update_in_progress = False        
        return data
    
    def diff_to_target(self, difficulty):
        '''Converts difficulty to target'''
        diff1 = 0x00000000ffff0000000000000000000000000000000000000000000000000000
        return diff1 / difficulty
    
    def get_job(self, job_id):
        '''For given job_id returns BlockTemplate instance or None'''
        try:
            j = self.jobs[job_id]
            #log.debug("j: %s", j)
        except:
            log.info("Job id '%s' not found" % job_id)
            return None
        
        # Now we have to check if job is still valid.
        # Unfortunately weak references are not bulletproof and
        # old reference can be found until next run of garbage collector.
        if j.prevhash_hex not in self.prevhashes:
            log.info("Prevhash of job '%s' is unknown" % job_id)
            return None
        
        if j not in self.prevhashes[j.prevhash_hex]:
            log.info("Job %s is unknown" % job_id)
            return None
        
        return j
        
    def submit_share(self, job_id, worker_name, session, extranonce1_bin, extranonce2, ntime, nonce,
                     difficulty):
        '''Check parameters and finalize block template. If it leads
           to valid block candidate, asynchronously submits the block
           back to the bitcoin network.
        
            - extranonce1_bin is binary. No checks performed, it should be from session data
            - job_id, extranonce2, ntime, nonce - in hex form sent by the client
            - difficulty - decimal number from session, again no checks performed
            - submitblock_callback - reference to method which receive result of submitblock()
        '''

        # Check if extranonce2 looks correctly. extranonce2 is in hex form...
        if len(extranonce2) != self.extranonce2_size * 2:
            raise SubmitException("Incorrect size of extranonce2. Expected %d chars" % (self.extranonce2_size*2))

        # normalize the case to prevent duplication of valid shares by the client
            ntime = ntime.lower()
            nonce = nonce.lower()
            extranonce2 = extranonce2.lower()

        # Check for job
        job = self.get_job(job_id)
        if job == None:
            raise SubmitException("Job '%s' not found" % job_id)
                
        # Check if ntime looks correct
        if len(ntime) != 8:
            raise SubmitException("Incorrect size of ntime. Expected 8 chars")

        if not job.check_ntime(int(ntime, 16)):
            raise SubmitException("Ntime out of range")
        
        # Check nonce
        if len(nonce) != 8:
            raise SubmitException("Incorrect size of nonce. Expected 8 chars")

        # Convert from hex to binary
        extranonce2_bin = binascii.unhexlify(extranonce2)
        ntime_bin = binascii.unhexlify(ntime)
        nonce_bin = binascii.unhexlify(nonce)

        # Check for duplicated submit
        if not job.register_submit(extranonce1_bin, extranonce2_bin, ntime_bin, nonce_bin):
            log.debug("Duplicate from %s, (%s %s %s %s)" % \
                    (worker_name, binascii.hexlify(extranonce1_bin), extranonce2, ntime, nonce))
            raise SubmitException("Duplicate share")
        
        # Now let's do the hard work!
        # ---------------------------

        # 1. Build coinbase
        coinbase_bin = job.serialize_coinbase(extranonce1_bin, extranonce2_bin)
        coinbase_hash = util.doublesha(coinbase_bin)
        
        # 2. Calculate merkle root
        merkle_root_bin = job.merkletree.withFirst(coinbase_hash)
        merkle_root_int = util.uint256_from_str(merkle_root_bin)
                
        # 3. Serialize header with given merkle, ntime and nonce
        header_bin = job.serialize_header(merkle_root_int, ntime_bin, nonce_bin)
        #log.debug("header_hex : %s", binascii.hexlify(''.join([ header_bin[i*4:i*4+4][::-1] for i in range(0, 46) ])))

        # 4. Reverse header and compare it with target of the user
        hash_bin = util.doublesha(''.join([ header_bin[i*4:i*4+4][::-1] for i in range(0, 46) ]))
        #log.debug("hash_bin   : %s", binascii.hexlify(hash_bin))

        hash_int = util.uint256_from_str(hash_bin)
        scrypt_hash_hex = "%064x" % hash_int
        header_hex = binascii.hexlify(header_bin)

        target_user = self.diff_to_target(difficulty)
        if settings.DEBUG:
            log.debug("hash_int   : %d", hash_int)
            log.debug("job.target : %d", job.target)
        if hash_int > target_user:
            raise SubmitException("Share is above target")

        # Algebra tells us the diff_to_target is the same as hash_to_diff
        share_diff = int(self.diff_to_target(hash_int))

        # 5. Compare hash with target of the network
        isBlockCandidate = False
        if hash_int <= job.target:
            isBlockCandidate = True

        if isBlockCandidate == True:
            # Yay! It is block candidate! 
            log.info("We found a block candidate! %s" % scrypt_hash_hex)

            # Reverse the header and get the potential block hash (for scrypt only)
            block_hash_bin = util.doublesha(''.join([ header_bin[i*4:i*4+4][::-1] for i in range(0, 46) ]))
            block_hash_hex = block_hash_bin[::-1].encode('hex_codec')

            # 6. Finalize and serialize block object 
            job.finalize(merkle_root_int, extranonce1_bin, extranonce2_bin, int(ntime, 16), int(nonce, 16))

            if not job.is_valid():
                # Should not happen
                log.exception("FINAL JOB VALIDATION FAILED!(Try enabling/disabling tx messages)")
                            
            # 7. Submit block to the network
            serialized = binascii.hexlify(job.serialize())
            #log.debug("serialized!!!: %s", serialized)
	    on_submit = self.bitcoin_rpc.submitblock(serialized, block_hash_hex, scrypt_hash_hex)
            if on_submit:
                self.update_block()

            if settings.SOLUTION_BLOCK_HASH:
                return (header_hex, block_hash_hex, share_diff, on_submit)
            else:
                return (header_hex, scrypt_hash_hex, share_diff, on_submit)
        
        if settings.SOLUTION_BLOCK_HASH:
        # Reverse the header and get the potential block hash (for scrypt only) only do this if we want to send in the block hash to the shares table
            block_hash_bin = util.doublesha(''.join([ header_bin[i*4:i*4+4][::-1] for i in range(0, 46) ]))
            block_hash_hex = block_hash_bin[::-1].encode('hex_codec')
            return (header_hex, block_hash_hex, share_diff, None)
        else:
            return (header_hex, scrypt_hash_hex, share_diff, None)
