# More compact unwind metadata on Linux

## Introduction

As many have suggested, compiling with frame pointers [1] is the lowest overhead and least complicated way to profile on
Linux. But recently there have been lots of discussions for alternatives to this. My personal case is to unwind Windows
applications without recompiling them; notably, with perf's `-g dwarf` recording mode a userspace tool can later read PE
unwind info and unwind the stack accurately; and having a flamegraph has helped me fix countless performance issues.

Most recently there has been discussion on SFrame, which presents itself as a simpler alternative to .eh_frame, that
runs fast, does not require stepping on CFI opcodes, and simple enough to implement as an in-kernel unwinder. I am very
excited that we might finally be able to unwind without recompiling for frame pointers. (Note, there needs to be some
way to generate SFrame, but it seems conversion is very much possible from DWARF and even PE.)

Now, as MaskRay [2] and other have highlighted, the size overhead of SFrame is at least as much as that of .eh_frame,
and this is large enough that it's a valid reason for people to turn this off for embedded or mobile OSes. So reducing
size is an important goal if SFrame wants to be successful.

## The prior arts

There are a few prior arts that we can draw ideas from.

The first is Mach-O's compact unwind format [3], which is fairly similar to a FRE in SFrame (which are expressions to
calculate old_rsp and old_rbp directly), except that it throws away all the FREs for prologs and epilogs and treat the
entire function as function body. I personally see this as "SFrame but with less accuracy and smaller size".

More interesting is PE's x64 and arm64 unwind code format [4,5]. The x64 format is represented in terms of "opcodes",
i.e. bytecode that maps 1:1 to HW instructions that are executed in a prolog. This has a size benefit since instructions
like push, which generates multiple CFI updates, can be compactly represented. In arm64 this idea is taken further to
also encode the length of the instruction itself into the opcode. On the downside, I think this is a lot of
(arch-specific) semantics that needs to be implemented by the unwinder, and recovering the rule requires stepping
through the opcodes, which is slower than having the FRE directly. It also imposes restrictions on how compilers can
emit the prolog (e.g. LLVM has some Windows-specific paths [6]).

It's also worth noting that PE unwind codes can do in-kernel async unwinds as well as C++-compatible full unwinds, with
the same metadata. This saves space.

The most interesting idea from arm64 unwind code format is canonical prologs as a common-path optimization. In addition
to constraining the instructions, the compiler needs to push and pop registers in a very specific order. On the bright
side, the unwind info for the entire function can now be represented with just (# of GP regs saved) + (# of FP regs
saved) + (stack alloc size) + (function length). This all fits in 32 bits and is extremely compact.

## Chunking CFIs

With the diversity of toolchains we have on Linux (and absence of a pre-existing restriction on prologs), it's unlikely
we can define a canonical prolog format. But this made me wonder how diverse are things in practice. With some simple
dwarfdump experiments, it seems that each compiler does push register in a fairly consistent order. And there is way
fewer unique CFI patterns compared to the number of FDEs we have.

Here are some sample numbers. I took the AMD Vulkan driver from Mesa, because it's fast to compile and contains a
diverse set of libraries.

| Binary              | Compiler | Frame Pointer | `.text` (bytes) | `.eh_frame_hdr` (bytes) | `.eh_frame` (bytes) | FDE count | .text/FDE (bytes) | FRE count | Unique CFA states |
|---------------------|----------|---------------|-----------------|-------------------------|---------------------|-----------|-------------------|-----------|-------------------|
| libvulkan_radeon.so | GCC      | No            | 7,141,795       | 116,244                 | 646,512             | 14,529    | 491               | 103,178   | 912               |
| libvulkan_radeon.so | GCC      | Yes           | 7,312,627       | 116,244                 | 520,888             | 14,529    | 503               | 60,024    | 39                |
| libvulkan_radeon.so | Clang    | No            | 6,562,563       | 68,604                  | 484,104             | 8,574     | 765               | 91,608    | 619               |
| libvulkan_radeon.so | Clang    | Yes           | 6,663,139       | 68,604                  | 394,240             | 8,574     | 777               | 54,982    | 17                |

My main idea is to have a registry of CFI chunks that is deduplicated, and build up the main binary search table from
these chunks. Remember, the number of unique CFI pattern is very low, so we can spend lots of bytes in the CFI itself,
making it self-describing and not needing to impose complexity on the unwinder or restrictions on the compiler.

As an example, using x86 assembly:

```
[chunk A --- prolog]
    .cfi_def_cfa_offset 8
    push %<reg> ; callee-saved reg
    .cfi_def_cfa_offset (offset from retaddr)
    ...

    sub $STACK_SIZE, %rsp
[chunk B --- inside function]
    .cfi_def_cfa_offset (offset from retaddr)
    .cfi_offset %<reg>, (offset from rsp) ; for all registers
    
    ... ; function body

    add $STACK_SIZE, %rsp
[chunk C --- epilog]
    .cfi_def_cfa_offset (offset from retaddr)
    pop %reg ; callee-saved reg
    .cfi_def_cfa_offset (offset from retaddr)
    ...
```

Chunk A and C is independent of stack size and can be reused across functions easily. Chunk B depends on the (saved
register, stack size), so we keep the # of duplicated rows minimal this way.

In the end state it will be up to the compiler to decide on a chunking scheme that deduplicates well. But until then,
external tool seeking to chunk on their own can use a simple heuristic: chunk at where the stack is deepest.

## How can this be adopted in SFrame?

The hierarchy of SFrame currently looks like:

- Header
- FDEs (fixed size: initial addr, size, FRE offset, FRE size, other metadata)
- FREs (variable size)

We could repurpose Function Descriptor Entries (FDEs) into Chunk Descriptor Entries (CDEs), since both represent an
array of FREs. The main difference is that a chunk is not associated with a particular code address, nor it has a
fixed code size (e.g. in the above example, chunk B corresponds to function body and have variable size). Size can be
defined implicitly by treating the next initial address as the end of current chunk.

So the end result would be breaking up FDEs into a two-level indirection:

- Header
- Addr-chunk table (fixed size: initial addr, CDE index)
- CDEs (fixed size; FRE offset, FRE size, other metadata)
- FREs (variable size)

The addr-chunk is most size-sensitive. Let's assume for now we use 32 bits for addr and 16 bits for CDE index. Assuming
an average function is chunked into A+B+C+Gap, that would be four chunk entries in the table, or 24 bytes per function.

Let's compare this to current SFrame, consider a function that does not establish FP, does not clobber FP and save N
non-FP callee-saved registers. The size per function is `20+(2+1)*(2*N+3)` bytes. To get a very rough ballpark estimate,
at N=4 SFrame would be 53 bytes, and the compressed scheme would be less than half of that.

There are further size optimization ideas I want to pursue. But that would be another long writing, so let's first
discuss whether this is a direction we want to go for, and we can come up with more ideas later.

## Appendix

### How to Extract Metrics

```
# Get section sizes
readelf -WS $file | awk '/\[.*\]/ && NF > 5 {print $2, "size:", strtonum("0x"$6)}'

# Get FRE count and unique CFA states
llvm-dwarfdump --eh-frame $file | ./extract_cfa_states.py

# Count FDEs
llvm-dwarfdump --eh-frame $file | grep -c '^[0-9a-f]* [0-9a-f]* [0-9a-f]* FDE'
```

[1]: https://www.brendangregg.com/blog/2024-03-17/the-return-of-the-frame-pointers.html

[2]: https://maskray.me/blog/2025-09-28-remarks-on-sframe

[3]: https://faultlore.com/blah/compact-unwinding/

[4]: https://learn.microsoft.com/en-us/cpp/build/exception-handling-x64

[5]: https://learn.microsoft.com/en-us/cpp/build/arm64-exception-handling

[6]: https://github.com/llvm/llvm-project/blob/main/llvm/lib/Target/X86/X86FrameLowering.cpp