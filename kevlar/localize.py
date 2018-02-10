#!/usr/bin/env python
#
# -----------------------------------------------------------------------------
# Copyright (c) 2017 The Regents of the University of California
#
# This file is part of kevlar (http://github.com/dib-lab/kevlar) and is
# licensed under the MIT license: see LICENSE.
# -----------------------------------------------------------------------------

from __future__ import print_function
from collections import defaultdict
from subprocess import Popen, PIPE, check_call
from tempfile import TemporaryFile
import os.path
import sys

import kevlar
import khmer
from khmer import CyclicCounttable as Counttable
import pysam


class KevlarBWAError(RuntimeError):
    """Raised if the delegated BWA call fails for any reason."""
    pass


class KevlarRefrSeqNotFoundError(ValueError):
    """Raised if the reference sequence cannot be found."""
    pass


class SeedMatchSet(object):
    """Store a set exact seed matches, indexed by sequence ID."""
    def __init__(self, seedsize):
        self._positions = defaultdict(list)
        self._seedsize = seedsize

    def __len__(self):
        return len(self._positions)

    def add(self, seqid, pos):
        self._positions[seqid].append(pos)

    def get_spans(self, seqid, clusterdist=10000):
        positions = sorted(self._positions[seqid])
        if len(positions) == 0:
            return None

        clusterspans = list()
        if clusterdist:
            cluster = list()
            for nextpos in positions:
                if len(cluster) == 0:
                    cluster.append(nextpos)
                    prevpos = nextpos
                    continue
                dist = nextpos - prevpos
                if dist > clusterdist:
                    if len(cluster) > 0:
                        span = (cluster[0], cluster[-1] + self._seedsize)
                        clusterspans.append(span)
                        cluster = list()
                cluster.append(nextpos)
                prevpos = nextpos
            if len(cluster) > 0:
                span = (cluster[0], cluster[-1] + self._seedsize)
                clusterspans.append(span)
            return clusterspans

        return [(positions[0], positions[-1] + self._seedsize)]

    @property
    def seqids(self):
        return set(list(self._positions.keys()))


def get_unique_seeds(recordstream, seedsize=31):
    """
    Grab all unique seeds from the specified sequence file.

    Input is expected to be an iterable containing screed or khmer sequence
    records.
    """
    ct = Counttable(seedsize, 1, 1)
    kmers = set()
    for record in recordstream:
        for kmer in ct.get_kmers(record.sequence):
            minkmer = kevlar.revcommin(kmer)
            if minkmer not in kmers:
                kmers.add(minkmer)
                yield kmer


def unique_seed_string(recordstream, seedsize=31):
    """
    Convert contigs to Fasta records of seed sequences for BWA input.

    Input is expected to be an iterable containing screed or khmer sequence
    records.
    """
    output = ''
    for n, kmer in enumerate(get_unique_seeds(recordstream, seedsize)):
        output += '>kmer{:d}\n{:s}\n'.format(n, kmer)
    return output


def get_exact_matches(contigstream, bwaindexfile, seedsize=31):
    """
    Compute a list of exact seed matches using BWA MEM.

    Input should be an iterable containing contigs generated by
    `kevlar assemble`. This function decomposes the contigs into their
    constituent seeds and searches for exact matches in the reference using
    `bwa mem`. This function is a generator, and yields tuples of
    (seqid, startpos) for each exact match found.
    """
    kmers = unique_seed_string(contigstream, seedsize)
    cmd = 'bwa mem -k {k} -T {k} {idx} -'.format(k=seedsize, idx=bwaindexfile)
    cmdargs = cmd.split(' ')
    with TemporaryFile() as samfile:
        bwaproc = Popen(cmdargs, stdin=PIPE, stdout=samfile, stderr=PIPE,
                        universal_newlines=True)
        stdout, stderr = bwaproc.communicate(input=kmers)
        if bwaproc.returncode != 0:  # pragma: no cover
            print(stderr, file=sys.stderr)
            raise KevlarBWAError('problem running BWA')
        samfile.seek(0)
        sam = pysam.AlignmentFile(samfile, 'r')
        for record in sam:
            if record.is_unmapped:
                continue
            seqid = sam.get_reference_name(record.reference_id)
            yield seqid, record.pos


def extract_regions(refr, seedmatches, delta=25, maxdiff=10000):
    """
    Extract the specified genomic region from the provided file object.

    The start and end parameters define a 0-based half-open genomic interval.
    Bounds checking must be performed on the end parameter.
    """
    observed_seqids = set()
    for defline, sequence in kevlar.seqio.parse_fasta(refr):
        seqid = defline[1:].split()[0]
        observed_seqids.add(seqid)

        regions = seedmatches.get_spans(seqid, maxdiff)
        if regions is None:
            continue

        for start, end in regions:
            newstart = max(start - delta, 0)
            newend = min(end + delta, len(sequence))
            subseqid = '{}_{}-{}'.format(seqid, newstart, newend)
            subseq = sequence[newstart:newend]
            yield subseqid, subseq

    missing = [s for s in seedmatches.seqids if s not in observed_seqids]
    if len(missing) > 0:
        raise KevlarRefrSeqNotFoundError(','.join(missing))


def autoindex(refrfile, logstream=sys.stderr):
    bwtfile = refrfile + '.bwt'
    if os.path.isfile(bwtfile):
        return

    message = 'WARNING: BWA index not found for "{:s}"'.format(refrfile)
    message += ', indexing now'
    print('[kevlar::localize]', message, file=logstream)

    try:
        check_call(['bwa', 'index', refrfile])
    except Exception as err:  # pragma: no cover
        raise KevlarBWAError('Could not run "bwa index"') from err


def localize(contigstream, refrfile, seedsize=31, delta=25, maxdiff=10000,
             logstream=sys.stderr):
    """
    Wrap the `kevlar localize` task as a generator.

    Input is an iterable containing contigs (assembled by `kevlar assemble`)
    stored as khmer or screed sequence records, the filename of the reference
    genome sequence, and the desired seed size.
    """
    autoindex(refrfile, logstream)
    seedmatches = SeedMatchSet(seedsize)
    for seqid, pos in get_exact_matches(contigstream, refrfile, seedsize):
        seedmatches.add(seqid, pos)
    if len(seedmatches) == 0:
        message = 'WARNING: no reference matches'
        print('[kevlar::localize]', message, file=logstream)
        return
    refrstream = kevlar.open(refrfile, 'r')
    for subseqid, subseq in extract_regions(refrstream, seedmatches,
                                            delta=delta, maxdiff=maxdiff):
        yield khmer.Read(name=subseqid, sequence=subseq)


def main(args):
    contigstream = kevlar.parse_augmented_fastx(kevlar.open(args.contigs, 'r'))
    outstream = kevlar.open(args.out, 'w')
    localizer = localize(
        contigstream, args.refr, seedsize=args.seed_size, delta=args.delta,
        maxdiff=args.max_diff, logstream=args.logfile
    )
    for record in localizer:
        khmer.utils.write_record(record, outstream)
