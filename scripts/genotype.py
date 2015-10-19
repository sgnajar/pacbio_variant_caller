"""
Genotype a set of SV calls in a given set of BAMs.
"""
import argparse
from collections import defaultdict
import csv
import intervaltree
import logging
import numpy as np
import operator
import pybedtools
import pysam

logging.basicConfig(filename="genotyper.log",level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Input file looks like this with SV coordinates in local assembly at columns 6-8.
# chr1    350793  350794  insertion       40      chr1-350756-362784|utg7180000000000|merged      37      77
CHROMOSOME=0
START=1
END=2
EVENT_TYPE=3
EVENT_LENGTH=4
CONTIG_NAME=5
CONTIG_START=6
CONTIG_END=7

BAM_CMATCH = 0
BAM_CSOFT_CLIP = 4
DEVIATIONS_FOR_THRESHOLD = 1.5
# Allow at most 2 mismatches in a 100 bp read.
MAX_ERROR_RATE = 0.02


def has_perfect_mapping(read):
    """
    Returns True if the given pysam read maps perfectly.

    A "perfect" mapping is either a mapping quality greater than zero and no
    more than 2% mismatches or a full-length alignment of the read without any
    mismatches.
    """
    return (
        not read.is_unmapped and
        ((read.mapq > 0 and dict(read.tags)["NM"] <= np.ceil(MAX_ERROR_RATE * read.qlen)) or
         (len(read.cigar) == 1 and read.cigar[0][0] == BAM_CMATCH and read.cigar[0][1] == read.qlen and dict(read.tags)["NM"] == 0))
    )


def spans_region(read, region):
    """
    Returns True if the given pysam read spans the given pybedtools.Interval,
    ``region``.
    """
    return read.reference_start <= region.start and read.reference_end >= region.end


def has_gaps_in_region(read, region):
    """
    Returns True if the given pysam read spans the given pybedtools.Interval,
    ``region``.
    """
    # If the given read has gaps in its alignment to the reference inside the
    # given interval (more than one block inside the SV event itself), there are
    # gaps inside the SV.
    tree = intervaltree.IntervalTree()
    for block in read.get_blocks():
        tree[block[0]:block[1]] = block

    return len(tree[region.start:region.end]) > 1


def pair_spans_regions(read_pair, regions):
    """
    Returns True if the given pysam reads spans the given pybedtools.Interval
    list, ``regions``. Read pairs can span the regions even if either read
    overlaps the region breakpoints.
    """
    return len(read_pair) == 2 and read_pair[0].reference_start < regions[0].start and read_pair[1].reference_end > regions[-1].end


def soft_clips_at_breakpoint(read, region):
    """
    Returns True if the given pysam read maps up to the edge of the given
    pybedtools.Interval, ``region`` and soft clips at that edge.

    Two cases are possible:

    1. The end of the read soft clips such that the read's reference end stops
    at the breakpoint start and the end of the read is softclipped.

    2. The beginning of the read soft clips such that the read's reference start
    begins at the breakpoint end and the beginning of the read is softclipped.

    In both cases, the read should be mapped in its best location in the
    reference ("perfect mapping").
    """
    return has_perfect_mapping(read) and (
        (read.reference_end == region.start and read.cigar[-1][0] == BAM_CSOFT_CLIP) or
        (read.reference_start == region.end + 1 and read.cigar[0][0] == BAM_CSOFT_CLIP)
    )


def maps_outside_regions(read, regions):
    """
    Returns True if the given pysam read maps outside the range of the given
    regions.
    """
    return (
        (read.reference_start < regions[0].start and read.reference_end < regions[0].start) or
        (read.reference_start > regions[-1].end and read.reference_end > regions[-1].end)
    )


def is_proper_pair(read, lower_insert_size_threshold, upper_insert_size_threshold):
    """
    Returns True if the given pysam read maps outside the range of the given
    regions.
    """
    return lower_insert_size_threshold <= np.abs(read.isize) <= upper_insert_size_threshold


def get_insert_sizes_for_region(bam, region):
    """
    Return insert sizes reads in the given pysam.AlignmentFile, ``bam``, and the
    given pybedtools.Interval, ``region``.
    """
    return [read.tlen for read in bam.fetch(region.chrom, region.start, region.end)
            if has_perfect_mapping(read) and not read.mate_is_unmapped and read.tlen >= 0 and read.tlen <= 1000]


def get_depth_for_region(bam_fields, regions, breakpoints, region_type="control"):
    """
    Return mean read depth for concordant and discordant reads in the given
    dictionary ``bam_fields`` with a pysam.AlignmentFile in the field "bam" and
    the given pybedtools.BedTool of regions, ``regions``.

    The depth calculated across the given regions depends on the type of region
    which is "control" by default. Other acceptable values for the region type
    are "insertion" or "deletion". If the region type is not "control" then the
    coordinates given by ``breakpoints`` are used to evaluate concordant and
    discordant read pairs.
    """
    bam = bam_fields["file"]
    lower_insert_threshold = bam_fields["lower_insert_threshold"]
    upper_insert_threshold = bam_fields["upper_insert_threshold"]

    # Convert breakpoints to a list for easier indexing and create an interval
    # for the complete span of the SV using the first breakpoint's start and
    # last breakpoint's end.
    breakpoints = list(breakpoints)
    sv_coordinates = pybedtools.Interval(breakpoints[0][0], int(breakpoints[0][1]), int(breakpoints[-1][2]))

    # Get all distinct reads in given regions.
    reads = set()
    for region in regions:
        for read in bam.fetch(region.chrom, region.start, region.end):
            # Exclude secondary and supplementary alignments (flags 0x100 and 0x800).
            if not read.is_secondary and (read.flag & 0x800 == 0):
                reads.add(read)

    # Group reads by read name.
    reads_by_name = defaultdict(list)
    for read in reads:
        reads_by_name[read.qname].append(read)

    logger.debug("Found %i potential read pairs for %s", len(reads_by_name), str(region).strip())

    concordant_pairs = []
    discordant_pairs = []

    for read_name, read_pair in reads_by_name.iteritems():
        # Sort reads by position to get reads "1" and "2" in expected order.
        read_pair = sorted(read_pair, key=operator.attrgetter("pos"))

        is_concordant = None
        if region_type == "insertion":
            # TODO: "First" read might be in reverse orientation if actual first read didn't map.
            if any([spans_region(read, sv_coordinates) and has_gaps_in_region(read, sv_coordinates) for read in read_pair]):
                is_concordant = False
                logger.debug("Discordant: read spans SV with gaps")
            elif any([soft_clips_at_breakpoint(read_pair[0], region) for region in breakpoints]) or (len(read_pair) == 2 and any([soft_clips_at_breakpoint(read_pair[1], region) for region in breakpoints])):
                is_concordant = False
                logger.debug("Discordant: soft clips at breakpoint")
            elif not read_pair[0].is_reverse and has_perfect_mapping(read_pair[0]) and spans_region(read_pair[0], breakpoints[0]):
                is_concordant = True
            elif len(read_pair) == 2 and read_pair[1].is_reverse and has_perfect_mapping(read_pair[1]) and spans_region(read_pair[1], breakpoints[-1]):
                is_concordant = True
            # Both reads are aligned with perfect mappings and orientation
            elif len(read_pair) == 2 and has_perfect_mapping(read_pair[0]) and has_perfect_mapping(read_pair[1]) and not read_pair[0].is_reverse and read_pair[1].is_reverse:
                # At least one read maps across a breakpoint or inside the SV.
                if (
                    (maps_outside_regions(read_pair[0], breakpoints) and not maps_outside_regions(read_pair[1], breakpoints)) or
                    (not maps_outside_regions(read_pair[0], breakpoints) and maps_outside_regions(read_pair[1], breakpoints)) or
                    (maps_outside_regions(read_pair[0], breakpoints) and spans_region(read_pair[1], breakpoints[0])) or
                    (spans_region(read_pair[0], breakpoints[-1]) and maps_outside_regions(read_pair[1], breakpoints))
                ):
                    is_concordant = True
                # Reads map too far apart from each other based on insert size thresholds.
                elif np.abs(read_pair[0].isize) > upper_insert_threshold:
                    logger.debug("Discordant: too far apart (%s > %s)", np.abs(read_pair[0].isize), upper_insert_threshold)
                    is_concordant = False
        elif region_type == "deletion":
            if len(read_pair) == 2 and has_perfect_mapping(read_pair[0]) and has_perfect_mapping(read_pair[1]) and not read_pair[0].is_reverse and read_pair[1].is_reverse and pair_spans_regions(read_pair, breakpoints) and is_proper_pair(read_pair[0], lower_insert_threshold, upper_insert_threshold):
                is_concordant = True
            elif len(read_pair) == 2 and has_perfect_mapping(read_pair[0]) and has_perfect_mapping(read_pair[1]) and not read_pair[0].is_reverse and read_pair[1].is_reverse and pair_spans_regions(read_pair, breakpoints) and np.abs(read_pair[0].isize) < lower_insert_threshold:
                is_concordant = False
                logger.debug("Discordant: too close together (%s < %s)", np.abs(read_pair[0].isize), lower_insert_threshold)
            elif has_perfect_mapping(read_pair[0]) and not read_pair[0].is_reverse and read_pair[0].reference_end < breakpoints[0].start and (len(read_pair) == 1 or read_pair[1].is_unmapped or read_pair[1].reference_id != read_pair[0].reference_id):
                is_concordant = False
                logger.debug("Discordant: one end anchored to left")
            elif len(read_pair) == 1 and has_perfect_mapping(read_pair[0]) and read_pair[0].is_reverse and read_pair[0].reference_start > breakpoints[-1].end:
                is_concordant = False
                logger.debug("Discordant: one end anchored to right")
            elif len(read_pair) == 2 and has_perfect_mapping(read_pair[1]) and read_pair[1].is_reverse and read_pair[1].reference_start > breakpoints[-1].end and (read_pair[0].is_unmapped or read_pair[0].reference_id != read_pair[1].reference_id):
                is_concordant = False
                logger.debug("Discordant: one end anchored to right")
            elif soft_clips_at_breakpoint(read_pair[0], breakpoints[0]) or (len(read_pair) == 2 and soft_clips_at_breakpoint(read_pair[1], breakpoints[0])):
                is_concordant = False
                logger.debug("Discordant: soft clips at breakpoint")
        else:
            # Control regions look for all properly paired reads with proper
            # orientation and ignore discordant reads.
            if len(read_pair) == 2 and has_perfect_mapping(read_pair[0]) and has_perfect_mapping(read_pair[1]) and not read_pair[0].is_reverse and read_pair[1].is_reverse and is_proper_pair(read_pair[0], lower_insert_threshold, upper_insert_threshold):
                is_concordant = True

        if is_concordant:
            concordant_pairs.append(read_pair)
        elif is_concordant is False:
            discordant_pairs.append(read_pair)

    return concordant_pairs, discordant_pairs


def genotype_call_with_read_pair(concordant, discordant, std_depth):
    """
    Genotype call based on total depth of concordant and discordant reads a la
    Hormozdiari et al. 2010 (Genome Research).
    """
    #homozygous_deletion_threshold = std_depth
    homozygous_deletion_threshold = 5

    if concordant < homozygous_deletion_threshold and discordant < homozygous_deletion_threshold:
        genotype = "./."
        discordant_genotype_likelihood = np.power(2, discordant) / np.power(2, homozygous_deletion_threshold)
        concordant_genotype_likelihood = np.power(2, concordant) / np.power(2, homozygous_deletion_threshold)
        shared_likelihood = np.sqrt(np.square(concordant_genotype_likelihood) + np.square(discordant_genotype_likelihood))
        genotype_likelihood = np.floor(-10 * np.log10(shared_likelihood))
    else:
        expected_discordant_lower_bound = concordant * 0.25
        expected_discordant_upper_bound = concordant * 4

        if discordant < expected_discordant_lower_bound:
            genotype = "1/1"
            # Calculate likelihood of homozygous alternate genotype as
            # Phred-scaled proportion of distance between the observed
            # discordant depth and expected depth for the corresponding
            # concordant depth.
            genotype_likelihood = np.floor(-10 * np.log10(np.power(2, discordant) / np.power(2, expected_discordant_lower_bound)))
        elif expected_discordant_lower_bound <= discordant < expected_discordant_upper_bound:
            # Calculate likelihood of heterozygous genotype as Phred-scaled
            # proportion of distance between the observed discordant depth and
            # expected depth for the corresponding concordant depth.
            genotype_ratio = np.power(2, np.abs(discordant - expected_discordant_upper_bound)) / np.power(2, expected_discordant_upper_bound)
            genotype_likelihood = np.floor(-10 * np.log10(1 - min(genotype_ratio, 1)))
            genotype = "1/0"
        else:
            # Calculate likelihood of homozygous "reference" genotype as
            # Phred-scaled proportion of distance between the observed
            # discordant depth and expected depth for the corresponding
            # concordant depth.
            genotype_ratio = np.power(2, np.abs(discordant - expected_discordant_upper_bound)) / np.power(2, expected_discordant_upper_bound)
            genotype_likelihood = np.floor(-10 * np.log10(1 - min(genotype_ratio, 1)))
            genotype = "0/0"

    return genotype, genotype_likelihood


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("sv_calls", help="BED file of SV calls with reference coordinates in columns 1-3, SV type in 4, length in 5, and contig coordinates in 6-8")
    parser.add_argument("control_regions", help="BED file of control regions with empirical copy number in column 4")
    parser.add_argument("bams", nargs="+", help="one or more BAMs per sample to genotype; read groups must be present in BAM")
    parser.add_argument("output", help="file to emit VCF output to; use /dev/stdout for piping")
    args = parser.parse_args()

    # Load control regions with copy number in column 4. Use copy number 2
    # regions.
    control_regions = pybedtools.BedTool(args.control_regions)
    logger.debug("Loaded %i control regions", len(control_regions))
    copy_2_regions = [region for region in control_regions if region.name == "2"]
    logger.debug("Found %i copy 2 regions", len(copy_2_regions))

    chromosome_sizes = None
    bams_by_name = defaultdict(dict)
    for bam in args.bams:
        bam_file = pysam.AlignmentFile(bam, "rb")

        # Prepare chromosome lengths based on one BAM's header.
        if chromosome_sizes is None:
            chromosome_sizes = dict(zip(bam_file.references, [(0, length) for length in bam_file.lengths]))
            logger.debug("Got %i chromosome sizes", len(chromosome_sizes))

        insert_sizes = []
        for control_region in copy_2_regions:
            #logging.debug("Get insert size distribution from copy 2 region %s", control_region)
            insert_sizes.extend(get_insert_sizes_for_region(bam_file, control_region))

        mean_insert_size = np.median(insert_sizes)
        std_insert_size = np.std(insert_sizes)
        lower_insert_threshold = int(mean_insert_size - (DEVIATIONS_FOR_THRESHOLD * std_insert_size))
        upper_insert_threshold = int(mean_insert_size + (DEVIATIONS_FOR_THRESHOLD * std_insert_size))

        logger.debug("Mean insert size at copy 2 regions for %s is %s", bam, mean_insert_size)
        logger.debug("Standard deviation of insert size at copy 2 regions for %s is %s", bam, std_insert_size)

        bams_by_name[bam]["file"] = bam_file
        bams_by_name[bam]["mean_insert_size"] = mean_insert_size
        bams_by_name[bam]["std_insert_size"] = std_insert_size
        bams_by_name[bam]["lower_insert_threshold"] = lower_insert_threshold
        bams_by_name[bam]["upper_insert_threshold"] = upper_insert_threshold

        # Calculate concordant support across all copy 2 regions.
        logger.debug("Calculating depth at copy 2 regions for %s", bam)
        # control_region_depths = []
        # for control_region in copy_2_regions:
        #     # Keep only the depth for concordant reads at the edge of each
        #     # control region plus or minus the mean insert size of this BAM's
        #     # library. This provides a baseline for SV breakpoints.
        #     breakpoint_intervals = [
        #         (control_region.chrom, control_region.start, control_region.start)
        #     ]

        #     breakpoints = pybedtools.BedTool(breakpoint_intervals).set_chromsizes(chromosome_sizes).slop(b=int(mean_insert_size)).merge()
        #     logging.debug("Testing copy 2 region %s", breakpoints)
        #     depth = get_depth_for_region(bams_by_name[bam], breakpoints, breakpoints)[0]
        #     control_region_depths.append(depth)

        # mean_depth = np.mean(control_region_depths)
        # std_depth = np.std(control_region_depths)
        # logger.debug("Mean depth at copy 2 regions for %s is %s", bam, mean_depth)
        # logger.debug("Standard deviation of depth at copy 2 regions for %s is %s", bam, std_depth)

        # bams_by_name[bam]["mean_depth"] = mean_depth
        # bams_by_name[bam]["std_depth"] = std_depth
        bams_by_name[bam]["std_depth"] = 5

        # # # Calculate mean read depth across all control regions.
        # # logger.debug("Calculating depth at control regions for %s", bam)
        # # #windowed_control_regions = control_regions.window_maker(b=control_regions, w=5000, i="src")
        # # control_region_depths = []
        # # for control_region in control_regions:
        # #     logging.debug("Testing control region %s", control_region)
        # #     depth = get_depth_for_region(bam_file, control_region)
        # #     control_region_depths.append((control_region.name, "%.2f" % depth))

        # # with open("control_depths.tab", "w") as oh:
        # #     control_writer = csv.writer(oh, delimiter="\t", lineterminator="\n")
        # #     control_writer.writerow(("copy", "depth"))
        # #     for depth in control_region_depths:
        # #         control_writer.writerow(depth)

    oh = open("concordant_support.tab", "w")
    concordant_writer = csv.writer(oh, delimiter="\t", lineterminator="\n")
    concordant_writer.writerow(("chr", "start", "end", "sv_call", "concordant", "discordant", "genotype", "genotype_likelihood"))

    concordant_reads = pysam.AlignmentFile("concordant_reads.bam", "wb", header=bams_by_name.values()[0]["file"].header)
    discordant_reads = pysam.AlignmentFile("discordant_reads.bam", "wb", header=bams_by_name.values()[0]["file"].header)

    with open(args.output, "w") as output_fh:
        writer = csv.writer(output_fh, delimiter="\t", lineterminator="\n")
        sv_calls = pybedtools.BedTool(args.sv_calls)

        for sv_call in sv_calls:
            if sv_call.name == "deletion":
                breakpoint_intervals = [
                    (sv_call[5], int(sv_call[6]), int(sv_call[7])),
                ]
            else:
                breakpoint_intervals = [
                    (sv_call[5], int(sv_call[6]), int(sv_call[6]) + 1),
                    (sv_call[5], int(sv_call[7]) - 1, int(sv_call[7]))
                ]
            logger.debug("Breakpoint intervals: %s", breakpoint_intervals)
            breakpoint_intervals = pybedtools.BedTool(breakpoint_intervals)

            for bam_name, bam in bams_by_name.iteritems():
                # Inspect either side of the SV breakpoint(s) by adding the mean
                # insert size of the read pairs to either side of each
                # breakpoint. Merge overlapping breakpoints to avoid double
                # counting support across adjacent breakpoints.
                regions = breakpoint_intervals.set_chromsizes(chromosome_sizes).slop(b=int(bam["mean_insert_size"])).merge()

                # Get concordant and discordant read pairs for all reads in the
                # given regions based on the breakpoint intervals of this SV.
                breakpoint_concordant, breakpoint_discordant = get_depth_for_region(bam, regions, breakpoint_intervals, sv_call.name)

                # Save concordant and discordant read pairs to their respective
                # BAMs.
                for read_pair in breakpoint_concordant:
                    for read in read_pair:
                        concordant_reads.write(read)

                for read_pair in breakpoint_discordant:
                    for read in read_pair:
                        discordant_reads.write(read)

                breakpoint_concordant_depth, breakpoint_discordant_depth = len(breakpoint_concordant), len(breakpoint_discordant)
                logger.debug("Found concordant depth for %s: %s", sv_call.name, breakpoint_concordant_depth)
                logger.debug("Found discordant depth for %s: %s", sv_call.name, breakpoint_discordant_depth)

                genotype, genotype_likelihood = genotype_call_with_read_pair(breakpoint_concordant_depth, breakpoint_discordant_depth, bam["std_depth"])
                logger.debug("%s had %s concordant, %s discordant, %s stddev for genotype of %s (GL: %s)", sv_call.name, breakpoint_concordant_depth, breakpoint_discordant_depth, bam["std_depth"], genotype, genotype_likelihood)

                concordant_writer.writerow(sv_call[-3:] + [sv_call.name] + ["%.2f" % value for value in (breakpoint_concordant_depth, breakpoint_discordant_depth)] + [genotype, genotype_likelihood])

            # variant_id = "_".join(row[:EVENT_LENGTH + 1])
            # reference_allele = "N"
            # alternate_allele = "<%s>" % row[EVENT_TYPE]
            # variant_quality = 30
            # filter_status = "PASS"
            # info_pairs = (("END", row[CONTIG_END]),)
            # info = ";".join(["=".join(pair) for pair in info_pairs])
            # format = "GT"

            # output_row = (
            #     row[CONTIG_NAME],
            #     row[CONTIG_START],
            #     variant_id,
            #     reference_allele,
            #     alternate_allele,
            #     variant_quality,
            #     filter_status,
            #     info,
            #     format
            # )

            #writer.writerow(output_row)

    oh.close()
    concordant_reads.close()
    discordant_reads.close()
