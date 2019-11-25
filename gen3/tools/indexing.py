"""
Module for indexing actions (typically against indexd's API). Supports
multiple processes using Python's multiprocessing library.

Attributes:
    INDEXD_RECORD_PAGE_SIZE (int): number of records to request per page
    TMP_FOLDER (str): Folder directory for placing temporary files
"""
import csv
import glob
import logging
from multiprocessing import Pool, Process, Manager, Queue
import multiprocessing
import os
import sys
import shutil
import math
import time
import urllib.parse

from gen3.index import Gen3Index

INDEXD_RECORD_PAGE_SIZE = 1024
TMP_FOLDER = os.path.abspath("./tmp") + "/"


def download_object_manifest(
    commons, output_filename="object-manifest.tsv", num_processes=5
):
    """
    Download all file object records into a manifest tsv

    Args:
        commons (str): root domain for commons where indexd
        output_filename (str, optional): filename for output
        num_processes (int, optional): number of parallel python processes to use for
          hitting indexd api and processing
    """
    start_time = time.time()
    logging.info(f"start time: {start_time}")

    # ensure tmp directory exists and is empty
    os.makedirs(TMP_FOLDER, exist_ok=True)
    for file in os.listdir(TMP_FOLDER):
        file_path = os.path.join(TMP_FOLDER, file)
        if os.path.isfile(file_path):
            os.unlink(file_path)

    _write_all_index_records_to_file(commons, output_filename, num_processes)

    end_time = time.time()
    logging.info(f"end time: {end_time}")
    logging.info(f"run time: {end_time-start_time}")


def _write_all_index_records_to_file(commons, output_filename, num_processes):
    """
    Spins up number of processes provided to parse indexd records and eventually
    write to a single output file manfiest.

    Args:
        commons (str): root domain for commons where indexd
        output_filename (str, optional): filename for output
        num_processes (int, optional): number of parallel python processes to use for
          hitting indexd api and processing

    Raises:
        IndexError: If script detects missing files in indexd after initial parsing
    """
    index = Gen3Index(commons)
    logging.debug(f"requesting indexd stats...")
    num_files = int(index.get_stats().get("fileCount"))
    # paging is 0-based, so subtract 1 from ceiling
    # note: float() is necessary to force Python 3 to not floor the result
    max_page = int(math.ceil(float(num_files) / INDEXD_RECORD_PAGE_SIZE)) - 1

    queue = Queue(max_page + num_processes)

    pages = [x for x in range(max_page)]
    _add_pages_to_queue_and_process(pages, queue, commons, num_processes)

    logging.info(f"checking if files were added since we started...")
    current_num_files = int(index.get_stats().get("fileCount"))

    # don't handle if files are actively being deleted
    if current_num_files < num_files:
        raise IndexError("Files were removed during pagination.")

    # if files we added we can try to parse them
    if current_num_files > num_files:
        logging.warning(
            f"current files {current_num_files} is not the same as when "
            f"we started {num_files}! Will attempt to get the new files but if more "
            "are ACTIVELY being added via the API this manifest WILL NOT BE COMPLETE."
        )

        new_extra_files = current_num_files - num_files
        new_pages_to_parse = int(
            math.ceil(float(new_extra_files) / INDEXD_RECORD_PAGE_SIZE)
        )

        # NOTE: start at previous max_page so we can pick up any addition files added to
        #       that page
        _add_pages_to_queue_and_process(
            [x for x in range(max_page, max_page + new_pages_to_parse)],
            queue,
            commons,
            num_processes,
        )

    logging.info(
        f"done processing queue, combining outputs to single file {output_filename}"
    )

    # remove existing output if it exists
    if os.path.isfile(output_filename):
        os.unlink(output_filename)

    with open(output_filename, "wb") as outfile:
        outfile.write("GUID, urls, authz, acl, md5, size\n".encode("utf8"))
        for filename in glob.glob(TMP_FOLDER + "*"):
            if output_filename == filename:
                # don't want to copy the output into the output
                continue
            logging.info(f"combining {filename} into {output_filename}")
            with open(filename, "rb") as readfile:
                shutil.copyfileobj(readfile, outfile)

    logging.info(f"done writing output to file {output_filename}")


def _add_pages_to_queue_and_process(pages, queue, commons, num_processes):
    """
    Adds the given pages to the queue and starts the number of processes
    provided to consume the queue.

    Args:
        pages (List[int]): list of page numbers to add to the queue
        queue (multiprocessing.Queue): thread-safe multi-producer/consumer queue
        commons (str): root domain for commons where indexd
        num_processes (int, optional): number of parallel python processes to use for
          hitting indexd api and processing
    """
    if pages:
        logging.debug(f"addings pages to queue. start: {pages[0]}, end {pages[-1]}")

    for page in pages:
        queue.put(page)

    logging.info(
        f"done adding to queue, sending {num_processes} STOP messages b/c {num_processes} processes"
    )

    for x in range(num_processes):
        queue.put("STOP")

    logging.info(f"starting {num_processes} processes..")

    processes = []
    for x in range(num_processes):
        p = Process(target=_get_page_and_write_records_to_file, args=(queue, commons))
        p.start()
        processes.append(p)

    logging.info(f"waiting for processes to finish processing queue...")

    for process in processes:
        process.join()


def _get_page_and_write_records_to_file(queue, commons):
    """
    Pops off queue until it sees a "STOP".
    Sends a request to get all records on a given popped queue page, parses the records,
    converts to manifest format, and writes to a tsv file in a tmp directory (all files
    will be combined later)

    Args:
        queue (multiprocessing.Queue): thread-safe multi-producer/consumer queue
        commons (str): root domain for commons where indexd
    """
    index = Gen3Index(commons)
    page = queue.get()
    process_name = multiprocessing.current_process().name
    while page != "STOP":
        records = index.get_records_on_page(page=page, limit=INDEXD_RECORD_PAGE_SIZE)

        logging.info(f"{process_name}:Read page {page} with {len(records)} records")

        if records:
            file_name = TMP_FOLDER + str(page) + ".tsv"
            with open(file_name, "w+", encoding="utf8") as file:
                logging.info(f"{process_name}:Write to {file_name}")
                tsvwriter = csv.writer(file, delimiter="\t")
                for record in records:
                    manifest_row = [
                        record.get("did"),
                        record.get("urls"),
                        record.get("authz"),
                        record.get("acl"),
                        record.get("md5"),
                        record.get("size"),
                    ]
                    tsvwriter.writerow(manifest_row)
        page = queue.get()

    logging.info(f"{process_name}:Stop")
