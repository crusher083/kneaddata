import os
import re
import sys
import time
import shutil
import logging
import itertools
import subprocess
import collections
from functools import partial
import gzip
import tempfile

from . import utilities
from . import config
from . import memoryheavy

# name global logging instance
logger=logging.getLogger(__name__)

def align(infile_list, db_prefix_list, output_prefix, tmp_dir, remove_temp_output,
          bowtie2_path, threads, processors, bowtie2_opts, verbose):

    """Align a single-end sequence file or a paired-end duo of sequence
    files using bowtie2.

    :param infile_list: List; Input sequence files in fastq format. A
                        list of length 1 is interpreted as a single-end
                        sequence file, while a list of length 2 is 
                        interpreted as a paired-end sequence duo.
    :param db_prefix_list: List; Bowtie2 database prefixes. Multiple database
                           can be specified. Uses subprocesses to run bowtie2 
                           in parallel.
    :param output_prefix: String; Prefix of the output file
    :param tmp_dir: String; Path name to the temporary output directory
    :param remove_temp_output: Boolean; If set, remove temp output
    :keyword bowtie2_path: String; File path to the bowtie2 binary's location.
    :keyword threads: Int; Number of threads to request for bowtie2 run.
    :keyword processes: Int; Number of bowtie2 subprocesses to run.
    :keyword bowtie2_opts: List; List of additional arguments, as strings, to 
                            be passed to Bowtie2.
    """

    # determine if the input are paired reads
    is_paired = (len(infile_list) == 2)

    # create the bowtie2 commands
    commands = []
    all_outputs_to_combine = []
    bowtie2_command = [bowtie2_path, "--threads", str(threads)] + bowtie2_opts
    
    for basename, fullpath in _prefix_bases(db_prefix_list):
        output_str = output_prefix + "_" + basename + "_bowtie2"
        cmd = bowtie2_command + ["-x", fullpath]
        if is_paired:
            cmd += ["-1", infile_list[0], "-2", infile_list[1],
                    "--al-conc", output_str + "_contam_%.fastq",
                    "--un-conc", output_str + "_clean_%.fastq"]
            outputs_to_combine = [output_str + "_clean_1.fastq", 
                                  output_str + "_clean_2.fastq"]
        else:
            cmd += ["-U", infile_list[0], "--al", output_str + "_contam.fastq",
                    "--un", output_str + "_clean.fastq"]
            outputs_to_combine = [output_str + "_clean.fastq"]

        if remove_temp_output:
            # if we are removing the temp output, then write the sam output to dev null to save space
            sam_out = os.devnull
        else:
            sam_out = os.path.join(tmp_dir, os.path.basename(output_str) + ".sam")
        cmd += [ "-S", sam_out ]
        
        commands.append([cmd,"bowtie2",infile_list,outputs_to_combine])
        all_outputs_to_combine.append(outputs_to_combine)

    # run the bowtie2 commands with the number of processes specified
    utilities.start_processes(commands,processors,verbose)
   
    # if bowtie2 produced output, merge the files from multiple databases
    combined_outs = []
    if all_outputs_to_combine:
        combined_outs = combine_fastq_output_files(all_outputs_to_combine, output_prefix, remove_temp_output)

    return combined_outs


def tag(infile_list, db_prefix_list, temp_dir, remove_temp_output, prefix,
        bmtagger_path, processes, verbose):
    """
    Runs BMTagger on a single-end sequence file or a paired-end duo of sequence
    files. Returns a tuple (ret_codes, bmt_args). Both are lists, each which has
    the same number of elements as the number of databases. THe first contains
    the return codes for each of the BMTagger executions, and the second
    contains the command line calls for these executions.

    :param infile_list: List; a list of input files in FASTQ format. A list of
                        length 1 is interpreted as a single-end sequence file,
                        while a list of length 2 is interpreted as a paired-end
                        sequence duo
    :param db_prefix_list: List; Prefixes of the BMTagger databases. BMTagger
                           needs databases of the format db_prefix.bitmask, 
                           db_prefix.srprism.*, and db_prefix.{blastdb file
                           extensions} for any fixed db_prefix (see
                           config.bmtagger_db_endings for full list of endings).
                           Multiple databases can be specified. Uses
                           subprocesses to run BMTagger in parallel
    :param temp_dir: String; Path name to temporary directory for BMTagger
    :param remove_temp_output: Boolean; If set, remove temp output
    :param prefix: String; prefix for output files
    :keyword bmtagger_path: String; file path to the bmtagger.sh executable
    :keyword processes: Int; Max number of BMTagger subprocesses to run
    """

    # determine if the input are paired reads
    is_paired = (len(infile_list) == 2)

    # create the bmtagger commands
    commands = []
    all_outputs_to_combine = []
    bmtagger_command = [bmtagger_path, "-q", "1", "-1", infile_list[0], 
                        "-T", temp_dir,"--extract"]

    # build arguments
    for (basename, fullpath) in _prefix_bases(db_prefix_list):
        out_prefix = prefix + "_" + basename + "_bmtagger_clean"
        cmd = bmtagger_command + ["-b", str(fullpath + ".bitmask"),
                                  "-x", str(fullpath + ".srprism"),
                                  "-o", out_prefix]
        if is_paired:
            cmd += ["-2", infile_list[1]]
            outputs_to_combine = [out_prefix + "_1.fastq",
                                  out_prefix + "_2.fastq"]
        else:
            outputs_to_combine = [out_prefix + ".fastq"]

        commands.append([cmd,"bmtagger",infile_list,outputs_to_combine])
        all_outputs_to_combine.append(outputs_to_combine)
        
    # run the bmtagger commands with the number of processes specified
    utilities.start_processes(commands,processes,verbose)

    # merge the output files from multiple databases
    combined_outs = []
    if all_outputs_to_combine:
        combined_outs = combine_fastq_output_files(all_outputs_to_combine, prefix, remove_temp_output)
                    
    return combined_outs

def intersect_fastq(lstrFiles, out_file):
    """
    Intersects multiple fastq files with one another. Includes only the reads (4
    lines long each) that are common to all the files. Writes these reads to the
    output file specified in out_file. 

    Input:
    lstrFiles:  a list of fastq file names (as strings)
    out_file:   output file to write the results. 
    """
    
    # optimize for the common case, where we are intersecting 1 file
    if len(lstrFiles) == 1:
        shutil.copyfile(lstrFiles[0], out_file)
        return

    counter = collections.Counter()
    for fname in lstrFiles:
        with open(fname, "rU") as f:
            # nifty trick to read 4 lines at a time (fastq files have 4 lines
            # per read)
            for lines in itertools.izip_longest(*[f]*4):
                try:
                    read = ("".join(lines)).strip()
                except TypeError:
                    message="Fastq file is not correctly formatted"
                    logger.critical(message)
                    sys.exit("ERROR:"+message+": "+ fname)
                else:
                    counter[read] += 1

    num_files = len(lstrFiles)
    with open(out_file, "w") as f:
        for key in counter:
            # only includes reads that are in n or more files, for n input files
            if counter[key] >= num_files:
                f.write(key+"\n")
    return

def combine_fastq_output_files(files_to_combine, out_prefix, remove_temp_output):
    """
    Summary: Combines fastq output if we run BMTagger/bowtie2 on multiple databases.
    Input:
        files_to_combine: a list of lists of BMTagger/bowtie2 outputs. The 'inner
        lists' are of length 1 or 2 with lists of 2 having output files for paired reads
        organized as [output for pair 1, output for pair 2]
        out_prefix: Prefix for the output files. 
        remove_temp_output: if true, then remove temp files after combine

    Output:
        Returns a list of output files. Additionally, the log file is updated
        with read counts for the different input and output files.
    """
    
    # print out the reads for all files
    utilities.log_read_count_for_files(files_to_combine,"Total reads after removing those found in reference database")

    # create lists of all of the output files for pair 1 and for pair 2
    files_for_pair1 = [f[0] for f in files_to_combine]
    try:
        files_for_pair2 = [f[1] for f in files_to_combine]
    except IndexError:
        files_for_pair2 = []

    # select an output prefix based on if the outputs are paired or not
    output_file = out_prefix + "_1.fastq"
    if not files_for_pair2:
        output_file = out_prefix + ".fastq"

    # create intersect file from all output files for pair 1
    intersect_fastq(files_for_pair1, output_file)
    output_files=[output_file]
    
    # create an intersect file from all output files for pair 2
    if files_for_pair2:
        output_file = out_prefix + "_2.fastq"
        intersect_fastq(files_for_pair2, output_file)
        output_files.append(output_file)

    # Get the read counts for the newly merged files
    utilities.log_read_count_for_files(output_files,"Total reads after merging results from multiple databases")

    # remove temp files if set
    if remove_temp_output:
        for group in [files_for_pair1, files_for_pair2]:
            for filename in group:
                logger.debug("Removing temporary file %s" %filename)
                try:
                    os.remove(filename)
                except EnvironmentError:
                    pass
                
    return output_files

def _prefix_bases(db_prefix_list):
    """From a list of absolute or relative paths, returns an iterator of the
    following tuple:
    (basename of all the files, full path of all the files)
    
    If more than one file as the same basename (but have different paths), the
    basenames get post-fixed with indices (starting from 0), based on how their
    basenames sort

    :param db_prefix_list: A list of databases' paths. Can be absolute or
    relative paths.
    """
    # sort by basename, but keep full path
    bases = sorted([ (os.path.basename(p), p) for p in db_prefix_list ], 
            key = lambda x: x[0])
    for name, group in itertools.groupby(bases, key=lambda x: x[0]):
        group = list(group)
        if len(group) > 1:
            for i, item in enumerate(group):
                yield ("%s_%i"%(item[0], i), item[1])
        else:
            yield (group[0][0], group[0][1])

def trim(infiles, outfiles_prefix, trimmomatic_path, quality_scores, 
         java_memory, additional_options, threads, verbose):
    """ 
    Create trimmomatic command based on input files and options and then run 
    Return a list of the output files
    """

    command = ["java", "-Xmx" + java_memory, "-d64", "-jar", trimmomatic_path]

    # determine if paired end input files
    paired_end=False
    if len(infiles) == 2:
        paired_end = True
        
    if paired_end:
        # set options for paired end input files
        mode = "PE"
        outfiles = [outfiles_prefix + config.trimomatic_pe_endings[0], 
                    outfiles_prefix + config.trimomatic_pe_endings[2],
                    outfiles_prefix + config.trimomatic_pe_endings[1],
                    outfiles_prefix + config.trimomatic_pe_endings[3]]
    else:
        # set options for single input file
        mode = "SE"
        outfiles = [outfiles_prefix + config.trimomatic_se_ending]
    
    # add positional arguments to command
    command += [mode, "-threads", str(threads), quality_scores] + infiles + outfiles
    # add optional arguments to command
    command += additional_options

    # run trimmomatic command
    utilities.run_command(command,"Trimmomatic",infiles,outfiles,verbose,exit_on_error=True)
    
    # now check all of the output files to find which are non-empty and return as 
    # sets for running the alignment steps
    nonempty_outfiles=[]
    outfile_size = [utilities.file_size(file) for file in outfiles]
    if paired_end:
        # if paired fastq files remain after trimming, preserve pairing
        if outfile_size[0] > 0 and outfile_size[1] > 0:
            nonempty_outfiles.append([outfiles[0],outfiles[1]])
        elif outfile_size[0] > 0:
            nonempty_outfiles.append([outfiles[0]])
        elif outfile_size[1] > 0:
            nonempty_outfiles.append([outfiles[1]])
        
        # add sequences without pairs, if present
        if outfile_size[2] > 0:
            nonempty_outfiles.append([outfiles[2]])
            
        if outfile_size[3] > 0:
            nonempty_outfiles.append([outfiles[3]])
        
    else:
        if outfile_size[0] > 0:
            nonempty_outfiles=[[outfiles[0]]]
        
    if not nonempty_outfiles:
        sys.exit("ERROR: Trimmomatic created empty output files.")
        
    return nonempty_outfiles


def run_trf(fastqs, outs, match, mismatch, delta, pm, pi, minscore, maxperiod, 
            generate_fastq, mask, html, trf_path, n_procs):
    nfiles = len(fastqs)
    # 1-2 files being passed in
    assert(nfiles == 2 or nfiles == 1)

    procs = list()
    names = list()
    for (fastq, out) in zip(fastqs, outs):
        proc = memoryheavy.run_tandem(fastq, out, match, mismatch, delta, pm,
                pi, minscore, maxperiod, generate_fastq,mask,html, trf_path)
        procs.append(proc)
        names.append("tandem.py on " + fastq)

    for (proc, name) in zip(procs, names):
        stdout, stderr = proc.communicate()
        retcode = proc.returncode
        utilities.process_return(name, retcode, stdout, stderr)
        
def decontaminate(args, output_prefix, files_to_align, tempdir):
    """
    Run bowtie2 or bmtagger then trf if set
    """

    # Start aligning
    message="Decontaminating ..."
    print(message)
    logger.info(message)
    possible_orphan = (len(files_to_align) > 1)
    orphan_count = 1
    for files_list in files_to_align:
        prefix = output_prefix
        if possible_orphan and (len(files_list) == 1):
            prefix = output_prefix + "_se_" + str(orphan_count)
            orphan_count += 1
        elif len(files_list) == 2:
            prefix = output_prefix + "_pe"
    
        trf_out_base = None
        if args.trf:
            trf_out_base = prefix
            prefix = prefix + "_pre_tandem"
    
        if args.bmtagger:
            c_outs = tag(files_list, args.reference_db, tempdir, 
                         args.remove_temp_output, prefix, args.bmtagger_path,
                         args.processes, args.verbose)
        else:
            c_outs = align(files_list, args.reference_db, prefix, tempdir, 
                           args.remove_temp_output, args.bowtie2_path, args.threads,
                           args.processes, args.bowtie2_options, args.verbose)

        
        # run TRF (within loop)
        # iterate over all outputs from combining (there should either be 1 or
        # 2)
        if args.trf:
            trf_outs = [trf_out_base]
            if len(c_outs) == 2:
                trf_outs = [trf_out_base + str(i + 1) for i in xrange(2)]
            run_trf(fastqs=c_outs,
                    outs=trf_outs,
                    match=args.match,
                    mismatch=args.mismatch,
                    delta=args.delta,
                    pm=args.pm,
                    pi=args.pi,
                    minscore=args.minscore,
                    maxperiod=args.maxperiod,
                    generate_fastq=args.no_generate_fastq,
                    mask=args.mask,
                    html=args.html,
                    trf_path=args.trf_path,
                    n_procs=args.processes)
            if args.remove_temp_output:
                for c_out in c_outs:
                    os.remove(c_out)
    

def storage_heavy(args):
    # set the prefix for the output files
    output_prefix = os.path.join(args.output_dir, args.output_prefix)
    
    # make temporary directory for temp output files
    if args.remove_temp_output:
        # create a temp folder if we are removing the temp output files
        tempdir=tempfile.mkdtemp(prefix=args.output_prefix+'_kneaddata_temp_',dir=args.output_dir)
    else:
        tempdir = output_prefix + "_temp"
        utilities.create_directory(tempdir)
    tempdir_output_prefix = os.path.join(tempdir, args.output_prefix)

    # Get the number of reads initially
    utilities.log_read_count_for_files(args.input,"Initial number of reads",args.verbose)

    # Set the location of the trimmomatic output files
    # Write to temp directory if alignment will also be run
    if args.reference_db:
        trimmomatic_files_prefix = tempdir_output_prefix
    else:
        trimmomatic_files_prefix = output_prefix

    # Run trimmomatic
    trimmomatic_output_files = trim(
        args.input, trimmomatic_files_prefix, args.trimmomatic_path, 
        args.trimmomatic_quality_scores, args.max_memory, args.trimmomatic_options, 
        args.threads, args.verbose)

    # Get the number of reads after trimming
    utilities.log_read_count_for_files(trimmomatic_output_files,"Total reads after trimming",args.verbose)

    # If a reference database is not provided, then bypass decontamination step
    if not args.reference_db:
        message="Bypass decontamination"
        logger.info(message)
        print(message)
    else:
        decontaminate(args, output_prefix, trimmomatic_output_files, tempdir)

    # Remove temp output files, if set to remove
    if args.remove_temp_output:
        logger.debug("Removing temporary files ...")
        try:
            shutil.rmtree(tempdir)
        except EnvironmentError:
            logger.debug("Unable to remove temp directory: " + tempdir)
    
