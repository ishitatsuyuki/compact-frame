#!/usr/bin/env python3
"""Extract and deduplicate CFA state rows from llvm-dwarfdump --eh-frame output."""

import sys
import re
import argparse
from collections import Counter

def extract_cfa_states(lines):
    """Extract lines that contain CFA state information (address: CFA=xxx: ...)."""
    cfa_pattern = re.compile(r'^\s+0x[0-9a-f]+:\s+CFA=')
    cfa_states = []

    for line in lines:
        if cfa_pattern.match(line):
            # Extract just the CFA state part (everything after the address)
            match = re.search(r'CFA=.*$', line)
            if match:
                cfa_states.append(match.group(0))

    return cfa_states

def normalize_rsp_offset(state):
    """
    Normalize CFA=RSP+offset to CFA=RSP+N to group states that differ only in RSP offset.
    Returns the normalized state and the original offset (or None if not RSP-based).
    """
    # Match CFA=RSP+<number> at the beginning
    match = re.match(r'CFA=RSP\+(\d+)(:.*)$', state)
    if match:
        offset = match.group(1)
        rest = match.group(2)
        normalized = f"CFA=RSP+N{rest}"
        return normalized, offset
    return state, None

def main():
    parser = argparse.ArgumentParser(
        description='Extract and analyze CFA states from llvm-dwarfdump --eh-frame output'
    )
    parser.add_argument('file', nargs='?', help='Input file (default: stdin)')
    parser.add_argument('--normalize-rsp', action='store_true',
                       help='Normalize CFA=RSP+offset entries to group by register pattern only')

    args = parser.parse_args()

    # Read from stdin or file
    if args.file:
        with open(args.file, 'r') as f:
            lines = f.readlines()
    else:
        lines = sys.stdin.readlines()

    # Extract CFA state lines
    cfa_states = extract_cfa_states(lines)

    print(f"Total CFA state rows: {len(cfa_states)}\n")

    # Count occurrences
    if args.normalize_rsp:
        # Normalize RSP offsets and track which offsets were seen
        normalized_states = []
        offset_info = {}  # Maps normalized state to set of offsets seen

        for state in cfa_states:
            normalized, offset = normalize_rsp_offset(state)
            normalized_states.append(normalized)

            if offset is not None:
                if normalized not in offset_info:
                    offset_info[normalized] = set()
                offset_info[normalized].add(offset)

        state_counts = Counter(normalized_states)

        print(f"Unique CFA states (with RSP offsets normalized): {len(state_counts)}\n")
        print("=" * 80)
        print("CFA States sorted by frequency (RSP+N means various offsets):")
        print("=" * 80)

        for state, count in state_counts.most_common():
            if state in offset_info:
                offsets = sorted(offset_info[state], key=int)
                offset_str = f" [offsets: {', '.join(offsets)}]"
                print(f"{count:6d}x  {state}{offset_str}")
            else:
                print(f"{count:6d}x  {state}")
    else:
        state_counts = Counter(cfa_states)

        print(f"Unique CFA states: {len(state_counts)}\n")
        print("=" * 80)
        print("CFA States sorted by frequency:")
        print("=" * 80)

        for state, count in state_counts.most_common():
            print(f"{count:6d}x  {state}")

    # Calculate redundancy
    total = len(cfa_states)
    unique = len(state_counts)
    if total > 0:
        redundancy = ((total - unique) / total) * 100
        print("\n" + "=" * 80)
        print(f"Redundancy: {redundancy:.2f}% ({total - unique} out of {total} rows are duplicates)")

if __name__ == '__main__':
    main()
