from Compiler.config import *
from Compiler.exceptions import *
from Compiler.instructions_base import RegType
import Compiler.instructions
import Compiler.instructions_base
import Compiler.instructions_base as inst_base
from . import compilerLib
from . import allocator as al
from . import util
import random
import time
import sys, os, errno
import inspect
from collections import defaultdict, deque
import itertools
import math
from functools import reduce
import re


data_types = dict(
    triple = 0,
    square = 1,
    bit = 2,
    inverse = 3,
    bittriple = 4,
    bitgf2ntriple = 5,
    dabit = 6,
)

field_types = dict(
    modp = 0,
    gf2n = 1,
    bit = 2,
)


class Program(object):
    """ A program consists of a list of tapes and a scheduled order
    of execution for these tapes.
    
    These are created by executing a file containing appropriate instructions
    and threads. """
    def __init__(self, args, options):
        self.options = options
        self.verbose = options.verbose
        self.args = args
        self.init_names(args)
        if sum(x != 0 for x in(options.ring, options.field,
                                               options.binary)) > 1:
            raise CompilerError('can only use one out of -B, -R, -F')
        if options.ring:
            self.bit_length = int(options.ring) - 1
        else:
            self.bit_length = int(options.binary) or int(options.field)
        if not self.bit_length:
            self.bit_length = 64
        print('Default bit length:', self.bit_length)
        self.security = 40
        print('Default security parameter:', self.security)
        self.galois_length = int(options.galois)
        if self.verbose:
            print('Galois length:', self.galois_length)
        self.tape_counter = 0
        self.tapes = []
        self._curr_tape = None
        self.DEBUG = False
        self.allocated_mem = RegType.create_dict(lambda: USER_MEM)
        self.free_mem_blocks = defaultdict(al.BlockAllocator)
        self.allocated_mem_blocks = {}
        self.saved = 0
        self.req_num = None
        self.tape_stack = []
        self.n_threads = 1
        self.free_threads = set()
        self.public_input_file = None
        self.types = {}
        self.budget = int(self.options.budget)
        self.to_merge = [Compiler.instructions.asm_open_class, \
                         Compiler.instructions.gasm_open_class, \
                         Compiler.instructions.muls_class, \
                         Compiler.instructions.gmuls_class, \
                         Compiler.instructions.mulrs_class, \
                         Compiler.instructions.gmulrs, \
                         Compiler.instructions.dotprods_class, \
                         Compiler.instructions.gdotprods_class, \
                         Compiler.instructions.asm_input_class, \
                         Compiler.instructions.gasm_input_class,
                         Compiler.instructions.inputfix_class,
                         Compiler.instructions.inputfloat_class,
                         Compiler.instructions.inputmixed_class,
                         Compiler.instructions.trunc_pr_class,
                         Compiler.instructions_base.Mergeable]
        import Compiler.GC.instructions as gc
        self.to_merge += [gc.ldmsdi, gc.stmsdi, gc.ldmsd, gc.stmsd, \
                          gc.stmsdci, gc.xors, gc.andrs, gc.ands, gc.inputb]
        self.use_trunc_pr = False
        self.use_dabit = options.mixed
        self._edabit = options.edabit
        self._split = False
        self._square = False
        self._always_raw = False
        Program.prog = self

    def get_args(self):
        return self.args

    def max_par_tapes(self):
        """ Upper bound on number of tapes that will be run in parallel.
        (Excludes empty tapes) """
        return self.n_threads
    
    def init_names(self, args):
        # ignore path to file - source must be in Programs/Source
        if 'Programs' in os.listdir(os.getcwd()):
            # compile prog in ./Programs/Source directory
            self.programs_dir = os.getcwd() + '/Programs'
        else:
            # assume source is in main SPDZ directory
            self.programs_dir = sys.path[0] + '/Programs'
        if self.verbose:
            print('Compiling program in', self.programs_dir)
        
        # create extra directories if needed
        for dirname in ['Public-Input', 'Bytecode', 'Schedules']:
            if not os.path.exists(self.programs_dir + '/' + dirname):
                os.mkdir(self.programs_dir + '/' + dirname)
        
        progname = args[0].split('/')[-1]
        if progname.endswith('.mpc'):
            progname = progname[:-4]
        
        if os.path.exists(args[0]):
            self.infile = args[0]
        else:
            self.infile = self.programs_dir + '/Source/' + progname + '.mpc'
        """
        self.name is input file name (minus extension) + any optional arguments.
        Used to generate output filenames
        """
        if self.options.outfile:
            self.name = self.options.outfile + '-' + progname
        else:
            self.name = progname
        if len(args) > 1:
            self.name += '-' + '-'.join(re.sub('/', '_', arg)
                                        for arg in args[1:])
        self.progname = progname

    def new_tape(self, function, args=[], name=None, single_thread=False):
        if name is None:
            name = function.__name__
        name = "%s-%s" % (self.name, name)
        # make sure there is a current tape
        self.curr_tape
        tape_index = len(self.tapes)
        self.tape_stack.append(self.curr_tape)
        self.curr_tape = Tape(name, self)
        self.curr_tape.prevent_direct_memory_write = not single_thread
        self.tapes.append(self.curr_tape)
        function(*args)
        self.finalize_tape(self.curr_tape)
        if self.tape_stack:
            self.curr_tape = self.tape_stack.pop()
        return tape_index

    def run_tape(self, tape_index, arg):
        return self.run_tapes([[tape_index, arg]])[0]

    def run_tapes(self, args):
        if self.curr_tape is not self.tapes[0]:
            raise CompilerError('Compiler does not support ' \
                                    'recursive spawning of threads')
        thread_numbers = []
        while len(thread_numbers) < len(args):
            if self.free_threads:
                thread_numbers.append(min(self.free_threads))
                self.free_threads.remove(thread_numbers[-1])
            else:
                thread_numbers.append(self.n_threads)
                self.n_threads += 1
        self.curr_tape.start_new_basicblock(name='pre-run_tape')
        Compiler.instructions.run_tape(*sum(([x] + list(y) for x, y in
                                             zip(thread_numbers, args)), []))
        self.curr_tape.start_new_basicblock(name='post-run_tape')
        for arg in args:
            self.curr_tape.req_node.children.append(
                self.tapes[arg[0]].req_tree)
        return thread_numbers

    def join_tape(self, thread_number):
        self.curr_tape.start_new_basicblock(name='pre-join_tape')
        Compiler.instructions.join_tape(thread_number)
        self.curr_tape.start_new_basicblock(name='post-join_tape')
        self.free_threads.add(thread_number)

    def update_req(self, tape):
        if self.req_num is None:
            self.req_num = tape.req_num
        else:
            self.req_num += tape.req_num
    
    def write_bytes(self, outfile=None):
        """ Write all non-empty threads and schedule to files. """

        nonempty_tapes = [t for t in self.tapes]

        sch_filename = self.programs_dir + '/Schedules/%s.sch' % self.name
        sch_file = open(sch_filename, 'w')
        print('Writing to', sch_filename)
        sch_file.write(str(self.max_par_tapes()) + '\n')
        sch_file.write(str(len(nonempty_tapes)) + '\n')
        sch_file.write(' '.join(tape.name for tape in nonempty_tapes) + '\n')
        sch_file.write('1 0\n')
        sch_file.write('0\n')
        sch_file.write(' '.join(sys.argv) + '\n')
        for tape in self.tapes:
            tape.write_bytes()

    def finalize_tape(self, tape):
        if not tape.purged:
            tape.optimize(self.options)
            tape.write_bytes()
            if self.options.asmoutfile:
                tape.write_str(self.options.asmoutfile + '-' + tape.name)
            tape.purge()
    
    @property
    def curr_tape(self):
        """ The tape that is currently running."""
        if self._curr_tape is None:
            assert not self.tapes
            self._curr_tape = Tape(self.name, self)
            self.tapes.append(self._curr_tape)
        return self._curr_tape

    @curr_tape.setter
    def curr_tape(self, value):
        self._curr_tape = value

    @property
    def curr_block(self):
        """ The basic block that is currently being created. """
        return self.curr_tape.active_basicblock
    
    def malloc(self, size, mem_type, reg_type=None):
        """ Allocate memory from the top """
        if not isinstance(size, int):
            raise CompilerError('size must be known at compile time')
        if size == 0:
            return
        if isinstance(mem_type, type):
            try:
                size *= math.ceil(mem_type.n / mem_type.unit)
            except AttributeError:
                pass
            self.types[mem_type.reg_type] = mem_type
            mem_type = mem_type.reg_type
        elif reg_type is not None:
            self.types[mem_type] = reg_type
        blocks = self.free_mem_blocks[mem_type]
        addr = blocks.pop(size)
        if addr is not None:
            self.saved += size
        else:
            addr = self.allocated_mem[mem_type]
            self.allocated_mem[mem_type] += size
            if len(str(addr)) != len(str(addr + size)) and self.verbose:
                print("Memory of type '%s' now of size %d" % (mem_type, addr + size))
        self.allocated_mem_blocks[addr,mem_type] = size
        return addr

    def free(self, addr, mem_type):
        """ Free memory """
        if self.curr_block.alloc_pool \
           is not self.curr_tape.basicblocks[0].alloc_pool:
            raise CompilerError('Cannot free memory within function block')
        size = self.allocated_mem_blocks.pop((addr,mem_type))
        self.free_mem_blocks[mem_type].push(addr, size)

    def finalize_memory(self):
        from . import library
        self.curr_tape.start_new_basicblock(None, 'memory-usage')
        # reset register counter to 0
        self.curr_tape.init_registers()
        for mem_type,size in list(self.allocated_mem.items()):
            if size:
                #print "Memory of type '%s' of size %d" % (mem_type, size)
                if mem_type in self.types:
                    self.types[mem_type].load_mem(size - 1, mem_type)
                else:
                    from Compiler.types import _get_type
                    _get_type(mem_type).load_mem(size - 1, mem_type)
        if self.verbose:
            if self.saved:
                print('Saved %s memory units through reallocation' % self.saved)

    def public_input(self, x):
        if self.public_input_file is None:
            self.public_input_file = open(self.programs_dir +
                                          '/Public-Input/%s' % self.name, 'w')
        self.public_input_file.write('%s\n' % str(x))

    def set_bit_length(self, bit_length):
        self.bit_length = bit_length
        print('Changed bit length for comparisons etc. to', bit_length)

    def set_security(self, security):
        self.security = security
        print('Changed statistical security for comparison etc. to', security)

    def optimize_for_gc(self):
        pass

    def get_tape_counter(self):
        res = self.tape_counter
        self.tape_counter += 1
        return res

    def use_edabit(self, change=None):
        if change is None:
            return self._edabit
        else:
            self._edabit = change

    def use_edabit_for(self, *args):
        return True

    def use_split(self, change=None):
        if change is None:
            return self._split
        else:
            assert change in (2, 3)
            self._split = change

    def use_square(self, change=None):
        if change is None:
            return self._square
        else:
            self._square = change

    def always_raw(self, change=None):
        if change is None:
            return self._always_raw
        else:
            self._always_raw = change

    def options_from_args(self):
        if 'trunc_pr' in self.args:
            self.use_trunc_pr = True
        if 'split' in self.args or 'split3' in self.args:
            self.use_split(3)
        if 'raw' in self.args:
            self.always_raw(True)

class Tape:
    """ A tape contains a list of basic blocks, onto which instructions are added. """
    def __init__(self, name, program):
        """ Set prime p and the initial instructions and registers. """
        self.program = program
        name += '-%d' % program.get_tape_counter()
        self.init_names(name)
        self.init_registers()
        self.req_tree = self.ReqNode(name)
        self.req_node = self.req_tree
        self.basicblocks = []
        self.purged = False
        self.block_counter = 0
        self.active_basicblock = None
        self.start_new_basicblock()
        self._is_empty = False
        self.merge_opens = True
        self.if_states = []
        self.req_bit_length = defaultdict(lambda: 0)
        self.function_basicblocks = {}
        self.functions = []
        self.prevent_direct_memory_write = False

    class BasicBlock(object):
        def __init__(self, parent, name, scope, exit_condition=None):
            self.parent = parent
            self.instructions = []
            self.name = name
            self.open_queue = []
            self.exit_condition = exit_condition
            self.exit_block = None
            self.previous_block = None
            self.scope = scope
            self.children = []
            if scope is not None:
                scope.children.append(self)
                self.alloc_pool = scope.alloc_pool
            else:
                self.alloc_pool = defaultdict(set)
            self.purged = False
            self.n_rounds = 0
            self.n_to_merge = 0

        def __len__(self):
            return len(self.instructions)

        def new_reg(self, reg_type, size=None):
            return self.parent.new_reg(reg_type, size=size)

        def set_return(self, previous_block, sub_block):
            self.previous_block = previous_block
            self.sub_block = sub_block

        def adjust_return(self):
            offset = self.sub_block.get_offset(self)
            self.previous_block.return_address_store.args[1] = offset
        
        def set_exit(self, condition, exit_true=None):
            """ Sets the block which we start from next, depending on the condition.

            (Default is to go to next block in the list)
            """
            self.exit_condition = condition
            self.exit_block = exit_true
            for reg in condition.get_used():
                reg.can_eliminate = False
        
        def add_jump(self):
            """ Add the jump for this block's exit condition to list of
            instructions (must be done after merging) """
            self.instructions.append(self.exit_condition)
        
        def get_offset(self, next_block):
            return next_block.offset - (self.offset + len(self.instructions))
        
        def adjust_jump(self):
            """ Set the correct relative jump offset """
            offset = self.get_offset(self.exit_block)
            self.exit_condition.set_relative_jump(offset)
            #print 'Basic block %d jumps to %d (%d)' % (next_block_index, jump_index, offset)

        def purge(self, retain_usage=True):
            def relevant(inst):
                req_node = Tape.ReqNode('')
                req_node.num = Tape.ReqNum()
                inst.add_usage(req_node)
                return req_node.num != {}
            if retain_usage:
                self.usage_instructions = list(filter(relevant,
                                                      self.instructions))
            else:
                self.usage_instructions = []
            if len(self.usage_instructions) > 1000:
                print('Retaining %d instructions' % len(self.usage_instructions))
            del self.instructions
            self.purged = True

        def add_usage(self, req_node):
            if self.purged:
                instructions = self.usage_instructions
            else:
                instructions = self.instructions
            for inst in instructions:
                inst.add_usage(req_node)
            req_node.num['all', 'round'] += self.n_rounds
            req_node.num['all', 'inv'] += self.n_to_merge

        def expand_cisc(self):
            new_instructions = []
            for inst in self.instructions:
                new_instructions.extend(inst.expand_merged())
                self.n_rounds += inst.expanded_rounds()
            self.instructions = new_instructions

        def __str__(self):
            return self.name

    def is_empty(self):
        """ Returns True if the list of basic blocks is empty.

        Note: False is returned even when tape only contains basic
        blocks with no instructions. However, these are removed when
        optimize is called. """
        if not self.purged:
            self._is_empty = (len(self.basicblocks) == 0)
        return self._is_empty

    def start_new_basicblock(self, scope=False, name=''):
        # use False because None means no scope
        if scope is False:
            scope = self.active_basicblock
        suffix = '%s-%d' % (name, self.block_counter)
        self.block_counter += 1
        sub = self.BasicBlock(self, self.name + '-' + suffix, scope)
        self.basicblocks.append(sub)
        self.active_basicblock = sub
        self.req_node.add_block(sub)
        #print 'Compiling basic block', sub.name

    def init_registers(self):
        self.reg_counter = RegType.create_dict(lambda: 0)
   
    def init_names(self, name):
        # ignore path to file - source must be in Programs/Source
        name = name.split('/')[-1]
        if name.endswith('.asm'):
            self.name = name[:-4]
        else:
            self.name = name
        self.infile = self.program.programs_dir + '/Source/' + self.name + '.asm'
        self.outfile = self.program.programs_dir + '/Bytecode/' + self.name + '.bc'

    def purge(self):
        for block in self.basicblocks:
            block.purge()
        self._is_empty = (len(self.basicblocks) == 0)
        del self.basicblocks
        del self.active_basicblock
        self.purged = True

    def unpurged(function):
        def wrapper(self, *args, **kwargs):
            if self.purged:
                return
            return function(self, *args, **kwargs)
        return wrapper

    @unpurged
    def optimize(self, options):
        if len(self.basicblocks) == 0:
            print('Tape %s is empty' % self.name)
            return

        if self.if_states:
            raise CompilerError('Unclosed if/else blocks')

        if self.program.verbose:
            print('Processing tape', self.name, 'with %d blocks' % len(self.basicblocks))

        for block in self.basicblocks:
            al.determine_scope(block, options)

        # merge open instructions
        # need to do this if there are several blocks
        if (options.merge_opens and self.merge_opens) or options.dead_code_elimination:
            for i,block in enumerate(self.basicblocks):
                if len(block.instructions) > 0 and self.program.verbose:
                    print('Processing basic block %s, %d/%d, %d instructions' % \
                        (block.name, i, len(self.basicblocks), \
                         len(block.instructions)))
                # the next call is necessary for allocation later even without merging
                merger = al.Merger(block, options, \
                                   tuple(self.program.to_merge))
                if options.dead_code_elimination:
                    if len(block.instructions) > 100000:
                        print('Eliminate dead code...')
                    merger.eliminate_dead_code()
                if options.merge_opens and self.merge_opens:
                    if len(block.instructions) == 0:
                        block.used_from_scope = util.set_by_id()
                        continue
                    if len(block.instructions) > 100000:
                        print('Merging instructions...')
                    numrounds = merger.longest_paths_merge()
                    block.n_rounds = numrounds
                    block.n_to_merge = len(merger.open_nodes)
                    if numrounds > 0 and self.program.verbose:
                        print('Program requires %d rounds of communication' % numrounds)
                    if merger.counter and self.program.verbose:
                        print('Block requires', \
                            ', '.join('%d %s' % (y, x.__name__) \
                                     for x, y in list(merger.counter.items())))
                # free memory
                merger = None
                if options.dead_code_elimination:
                    block.instructions = [x for x in block.instructions if x is not None]
        if not (options.merge_opens and self.merge_opens):
            print('Not merging instructions in tape %s' % self.name)

        if options.cisc:
            self.expand_cisc()

        # add jumps
        offset = 0
        for block in self.basicblocks:
            if block.exit_condition is not None:
                block.add_jump()
            block.offset = offset
            offset += len(block.instructions)
        for block in self.basicblocks:
            if block.exit_block is not None:
                block.adjust_jump()
            if block.previous_block is not None:
                block.adjust_return()

        # now remove any empty blocks (must be done after setting jumps)
        self.basicblocks = [x for x in self.basicblocks if len(x.instructions) != 0]

        # allocate registers
        reg_counts = self.count_regs()
        if not options.noreallocate:
            if self.program.verbose:
                print('Tape register usage before re-allocation:',
                      dict(reg_counts))
                print('modp: %d clear, %d secret' % (reg_counts[RegType.ClearModp], reg_counts[RegType.SecretModp]))
                print('GF2N: %d clear, %d secret' % (reg_counts[RegType.ClearGF2N], reg_counts[RegType.SecretGF2N]))
                print('Re-allocating...')
            allocator = al.StraightlineAllocator(REG_MAX, self.program)
            def alloc(block):
                for reg in sorted(block.used_from_scope, 
                                  key=lambda x: (x.reg_type, x.i)):
                    allocator.alloc_reg(reg, block.alloc_pool)
            def alloc_loop(block):
                left = deque([block])
                while left:
                    block = left.popleft()
                    alloc(block)
                    for child in block.children:
                        if child.instructions:
                            left.append(child)
            for i,block in enumerate(reversed(self.basicblocks)):
                if len(block.instructions) > 100000:
                    print('Allocating %s, %d/%d' % \
                        (block.name, i, len(self.basicblocks)))
                if block.exit_condition is not None:
                    jump = block.exit_condition.get_relative_jump()
                    if isinstance(jump, int) and jump < 0 and \
                            block.exit_block.scope is not None:
                        alloc_loop(block.exit_block.scope)
                allocator.process(block.instructions, block.alloc_pool)
            allocator.finalize(options)
            if self.program.verbose:
                print('Tape register usage:', dict(allocator.usage))

        # offline data requirements
        if self.program.verbose:
            print('Compile offline data requirements...')
        self.req_num = self.req_tree.aggregate()
        if self.program.verbose:
            print('Tape requires', self.req_num)
        for req,num in sorted(self.req_num.items()):
            if num == float('inf') or num >= 2 ** 32:
                num = -1
            if req[1] in data_types:
                self.basicblocks[-1].instructions.append(
                    Compiler.instructions.use(field_types[req[0]], \
                                                  data_types[req[1]], num, \
                                                  add_to_prog=False))
            elif req[1] == 'input':
                self.basicblocks[-1].instructions.append(
                    Compiler.instructions.use_inp(field_types[req[0]], \
                                                      req[2], num, \
                                                      add_to_prog=False))
            elif req[0] == 'modp':
                self.basicblocks[-1].instructions.append(
                    Compiler.instructions.use_prep(req[1], num, \
                                                   add_to_prog=False))
            elif req[0] == 'gf2n':
                self.basicblocks[-1].instructions.append(
                    Compiler.instructions.guse_prep(req[1], num, \
                                                    add_to_prog=False))
            elif req[0] == 'edabit':
                self.basicblocks[-1].instructions.append(
                    Compiler.instructions.use_edabit(False, req[1], num, \
                                                     add_to_prog=False))
            elif req[0] == 'sedabit':
                self.basicblocks[-1].instructions.append(
                    Compiler.instructions.use_edabit(True, req[1], num, \
                                                     add_to_prog=False))

        if not self.is_empty():
            # bit length requirement
            for x in ('p', '2'):
                if self.req_bit_length[x]:
                    bl = self.req_bit_length[x]
                    if self.program.options.ring:
                        bl = -int(self.program.options.ring)
                    self.basicblocks[-1].instructions.append(
                        Compiler.instructions.reqbl(bl,
                                                    add_to_prog=False))
            if self.program.verbose:
                print('Tape requires prime bit length', self.req_bit_length['p'])
                print('Tape requires galois bit length', self.req_bit_length['2'])

    @unpurged
    def expand_cisc(self):
        for block in self.basicblocks:
            block.expand_cisc()

    @unpurged
    def _get_instructions(self):
        return itertools.chain.\
            from_iterable(b.instructions for b in self.basicblocks)

    @unpurged
    def get_encoding(self):
        """ Get the encoding of the program, in human-readable format. """
        return [i.get_encoding() for i in self._get_instructions() if i is not None]
    
    @unpurged
    def get_bytes(self):
        """ Get the byte encoding of the program as an actual string of bytes. """
        return b"".join(i.get_bytes() for i in self._get_instructions() if i is not None)
    
    @unpurged
    def write_encoding(self, filename):
        """ Write the readable encoding to a file. """
        print('Writing to', filename)
        f = open(filename, 'w')
        for line in self.get_encoding():
            f.write(str(line) + '\n')
        f.close()
    
    @unpurged
    def write_str(self, filename):
        """ Write the sequence of instructions to a file. """
        print('Writing to', filename)
        f = open(filename, 'w')
        n = 0
        for block in self.basicblocks:
            if block.instructions:
                f.write('# %s\n' % block.name)
                for line in block.instructions:
                    f.write('%s # %d\n' % (line, n))
                    n += 1
        f.close()
    
    @unpurged
    def write_bytes(self, filename=None):
        """ Write the program's byte encoding to a file. """
        if filename is None:
            filename = self.outfile
        if not filename.endswith('.bc'):
            filename += '.bc'
        if not 'Bytecode' in filename:
            filename = self.program.programs_dir + '/Bytecode/' + filename
        print('Writing to', filename)
        f = open(filename, 'wb')
        f.write(self.get_bytes())
        f.close()
    
    def new_reg(self, reg_type, size=None):
        return self.Register(reg_type, self, size=size)
    
    def count_regs(self, reg_type=None):
        if reg_type is None:
            return self.reg_counter
        else:
            return self.reg_counter[reg_type]
    
    def __str__(self):
        return self.name

    class ReqNum(defaultdict):
        def __init__(self, init={}):
            super(Tape.ReqNum, self).__init__(lambda: 0, init)
        def __add__(self, other):
            res = Tape.ReqNum()
            for i,count in list(self.items()):
                res[i] += count            
            for i,count in list(other.items()):
                res[i] += count
            return res
        def __mul__(self, other):
            res = Tape.ReqNum()
            for i in self:
                res[i] = other * self[i]
            return res
        __rmul__ = __mul__
        def set_all(self, value):
            if value == float('inf') and self['all', 'inv'] > 0:
                print('Going to unknown from %s' % self)
            res = Tape.ReqNum()
            for i in self:
                res[i] = value
            return res
        def max(self, other):
            res = Tape.ReqNum()
            for i in self:
                res[i] = max(self[i], other[i])
            for i in other:
                res[i] = max(self[i], other[i])
            return res
        def cost(self):
            return sum(num * COST[req[0]][req[1]] for req,num in list(self.items()) \
                       if req[1] != 'input' and req[0] != 'edabit')
        def pretty(self):
            t = lambda x: 'integer' if x == 'modp' else x
            res = []
            for req, num in self.items():
                domain = t(req[0])
                n = '%12.0f' % num
                if req[1] == 'input':
                    res += ['%s %s inputs from player %d' \
                            % (n, domain, req[2])]
                elif domain.endswith('edabit'):
                    if domain == 'sedabit':
                        eda = 'strict edabits'
                    else:
                        eda = 'loose edabits'
                    res += ['%s %s of length %d' % (n, eda, req[1])]
                elif req[0] != 'all':
                    res += ['%s %s %ss' % (n, domain, req[1])]
            if self['all','round']:
                res += ['% 12.0f virtual machine rounds' % self['all','round']]
            return res
        def __str__(self):
            return ', '.join(self.pretty())
        def __repr__(self):
            return repr(dict(self))

    class ReqNode(object):
        __slots__ = ['num', 'children', 'name', 'blocks']
        def __init__(self, name):
            self.children = []
            self.name = name
            self.blocks = []
        def aggregate(self, *args):
            self.num = Tape.ReqNum()
            for block in self.blocks:
                block.add_usage(self)
            res = reduce(lambda x,y: x + y.aggregate(self.name),
                         self.children, self.num)
            return res
        def increment(self, data_type, num=1):
            self.num[data_type] += num
        def add_block(self, block):
            self.blocks.append(block)

    class ReqChild(object):
        __slots__ = ['aggregator', 'nodes', 'parent']
        def __init__(self, aggregator, parent):
            self.aggregator = aggregator
            self.nodes = []
            self.parent = parent
        def aggregate(self, name):
            res = self.aggregator([node.aggregate() for node in self.nodes])
            try:
                n_reps = self.aggregator([1])
                n_rounds = res['all', 'round']
                n_invs = res['all', 'inv']
                if (n_invs / n_rounds) * 1000 < n_reps:
                    print(self.nodes[0].blocks[0].name, 'blowing up rounds: ', \
                        '(%d / %d) ** 3 < %d' % (n_rounds, n_reps, n_invs))
            except:
                pass
            return res
        def add_node(self, tape, name):
            new_node = Tape.ReqNode(name)
            self.nodes.append(new_node)
            tape.req_node = new_node

    def open_scope(self, aggregator, scope=False, name=''):
        child = self.ReqChild(aggregator, self.req_node)
        self.req_node.children.append(child)
        child.add_node(self, '%s-%d' % (name, len(self.basicblocks)))
        self.start_new_basicblock(name=name)
        return child

    def close_scope(self, outer_scope, parent_req_node, name):
        self.req_node = parent_req_node
        self.start_new_basicblock(outer_scope, name)

    def require_bit_length(self, bit_length, t='p'):
        if t == 'p':
            self.req_bit_length[t] = max(bit_length + 1, \
                                         self.req_bit_length[t])
        else:
            self.req_bit_length[t] = max(bit_length, self.req_bit_length)

    class Register(object):
        """
        Class for creating new registers. The register's index is automatically assigned
        based on the block's  reg_counter dictionary.
        """
        __slots__ = ["reg_type", "program", "absolute_i", "relative_i", \
                         "size", "vector", "vectorbase", "caller", \
                         "can_eliminate", "duplicates"]
        maximum_size = 2 ** (32 - inst_base.Instruction.code_length) - 1

        def __init__(self, reg_type, program, size=None, i=None):
            """ Creates a new register.
                reg_type must be one of those defined in RegType. """
            if Compiler.instructions_base.get_global_instruction_type() == 'gf2n':
                if reg_type == RegType.ClearModp:
                    reg_type = RegType.ClearGF2N
                elif reg_type == RegType.SecretModp:
                    reg_type = RegType.SecretGF2N
            self.reg_type = reg_type
            self.program = program
            if size is None:
                size = Compiler.instructions_base.get_global_vector_size()
            if size is not None and size > self.maximum_size:
                raise CompilerError('vector too large')
            self.size = size
            self.vectorbase = self
            self.relative_i = 0
            if i is not None:
                self.i = i
            elif size is not None:
                self.i = program.reg_counter[reg_type]
                program.reg_counter[reg_type] += size
            else:
                self.i = float('inf')
            self.vector = []
            self.can_eliminate = True
            self.duplicates = util.set_by_id([self])
            if Program.prog.DEBUG:
                self.caller = [frame[1:] for frame in inspect.stack()[1:]]
            else:
                self.caller = None
            if self.i % 1000000 == 0 and self.i > 0:
                print("Initialized %d registers at" % self.i, time.asctime())

        @property
        def i(self):
            return self.vectorbase.absolute_i + self.relative_i

        @i.setter
        def i(self, value):
            self.vectorbase.absolute_i = value - self.relative_i

        def set_size(self, size):
            if self.size == size:
                return
            else:
                raise CompilerError('Mismatch of instruction and register size:'
                                    ' %s != %s' % (self.size, size))

        def set_vectorbase(self, vectorbase):
            if self.vectorbase is not self:
                raise CompilerError('Cannot assign one register' \
                                        'to several vectors')
            self.relative_i = self.i - vectorbase.i
            self.vectorbase = vectorbase

        def _new_by_number(self, i, size=1):
            return Tape.Register(self.reg_type, self.program, size=size, i=i)

        def get_vector(self, base, size):
            if base == 0 and size == self.size:
                return self
            if size == 1:
                return self[base]
            res = self._new_by_number(self.i + base, size=size)
            res.set_vectorbase(self)
            self.create_vector_elements()
            res.vector = self.vector[base:base+size]
            return res

        def create_vector_elements(self):
            if self.vector:
                return
            elif self.size == 1:
                self.vector = [self]
                return
            self.vector = []
            for i in range(self.size):
                reg = self._new_by_number(self.i + i)
                reg.set_vectorbase(self)
                self.vector.append(reg)

        def get_all(self):
            return self.vector or [self]

        def __getitem__(self, index):
            if not self.vector:
                self.create_vector_elements()
            return self.vector[index]

        def __len__(self):
            return self.size

        def copy(self):
            return Tape.Register(self.reg_type, Program.prog.curr_tape)

        def link(self, other):
            self.duplicates |= other.duplicates
            for dup in self.duplicates:
                dup.duplicates = self.duplicates

        @property
        def is_gf2n(self):
            return self.reg_type == RegType.ClearGF2N or \
                self.reg_type == RegType.SecretGF2N
        
        @property
        def is_clear(self):
            return self.reg_type == RegType.ClearModp or \
                self.reg_type == RegType.ClearGF2N or \
                self.reg_type == RegType.ClearInt

        def __bool__(self):
            raise CompilerError('cannot derive truth value from register')

        def __str__(self):
            return self.reg_type + str(self.i)

        __repr__ = __str__
