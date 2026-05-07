import os
import re
import subprocess
import logging

from installed_clients.ReadsUtilsClient import ReadsUtils
from installed_clients.AssemblyUtilClient import AssemblyUtil
from installed_clients.KBaseReportClient import KBaseReport

from kb_canu.utils.reads_file_utils import validate_reads_file
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Read-type flag mapping:
#   user-facing value  ->  canu CLI flag
# ---------------------------------------------------------------------------
READ_TYPE_FLAGS = {
    "nanopore":    "-nanopore",
    "pacbio-raw":  "-pacbio",
    "pacbio-hifi": "-pacbio-hifi",
}

# For HiFi data Canu skips correction; surfaced in report messages.
HIFI_READ_TYPE = "pacbio-hifi"


class CanuUtil:
    """Utility class that wraps the Canu assembler for KBase."""

    def __init__(self, config):
        """
        Parameters
        ----------
        config : dict
            Standard KBase SDK callback/config dict containing keys such as
            'callback_url', 'scratch', 'workspace-url', etc.
        """
        self.callback_url = config["SDK_CALLBACK_URL"]
        self.scratch = config["scratch"]
        self.workspace_url = config["workspace-url"]
        self.token = config.get("KB_AUTH_TOKEN", os.environ.get("KB_AUTH_TOKEN"))

        self.ru = ReadsUtils(self.callback_url, token=self.token)
        self.au = AssemblyUtil(self.callback_url, token=self.token)
        self.kbr = KBaseReport(self.callback_url, token=self.token)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run_kb_canu(self, params):
        """
        Top-level method called from kb_canuImpl.py.

        Parameters
        ----------
        params : dict
            Validated parameter dict matching CanuAssemblyParams in the KIDL
            spec.

        Returns
        -------
        dict
            {'report_name': str, 'report_ref': str}
        """
        self._validate_params(params)

        workspace_name = params["workspace_name"]
        reads_ref = params["reads_ref"]
        read_type = params["read_type"]
        output_name = params["output_assembly_name"]

        # ---- 1. Resolve reads: handle both single library and ReadsSet ------
        logger.info("Resolving reads input: %s", reads_ref)
        obj_type = self._get_object_type(reads_ref)
        logger.info("Object type: %s", obj_type)

        if "KBaseSets.ReadsSet" in obj_type:
            reads_info = self._download_reads_set(reads_ref, workspace_name)
        else:
            reads_info = self._download_reads(reads_ref)

        reads_files = reads_info["reads_files"]   # always a list of paths
        read_count  = reads_info.get("read_count", "unknown")
        n_libraries = reads_info.get("n_libraries", 1)
        logger.info("Downloaded %d read file(s) covering %s reads",
                    n_libraries, read_count)

        # ---- Validate every downloaded file before doing any work ----------
        for fpath in reads_files:
            validate_reads_file(fpath)

        # ---- 2. Merge reads files if more than one library ------------------
        assembly_prefix = "canu_assembly"
        work_dir = os.path.join(self.scratch, "canu_run")
        os.makedirs(work_dir, exist_ok=True)

        if n_libraries > 1:
            merged_file = os.path.join(work_dir, "merged_reads.fastq.gz")
            logger.info("Merging %d libraries into %s", n_libraries, merged_file)
            self._merge_reads_files(reads_files, merged_file)
            reads_file = merged_file
        else:
            reads_file = reads_files[0]

        # ---- 3. Build and run Canu command ----------------------------------
        cmd = self._build_canu_command(
            params=params,
            reads_file=reads_file,
            assembly_prefix=assembly_prefix,
            work_dir=work_dir,
        )

        logger.info("Running Canu: %s", " ".join(cmd))
        self._run_command(cmd)

        # ---- 4. Locate output contigs FASTA ---------------------------------
        contigs_file = self._find_contigs_fasta(work_dir, assembly_prefix)
        logger.info("Canu output contigs: %s", contigs_file)

        # ---- 5. Upload Assembly object with provenance ----------------------
        assembly_ref = self._upload_assembly(
            workspace_name=workspace_name,
            contigs_file=contigs_file,
            assembly_name=output_name,
            reads_ref=reads_ref,
            params=params,
        )
        logger.info("Uploaded assembly: %s", assembly_ref)

        # ---- 6. Parse assembly statistics -----------------------------------
        stats = self._parse_assembly_stats(work_dir, assembly_prefix)

        # ---- 7. Build HTML report -------------------------------------------
        report_info = self._create_report(
            workspace_name=workspace_name,
            assembly_ref=assembly_ref,
            assembly_name=output_name,
            stats=stats,
            read_type=read_type,
            read_count=read_count,
            n_libraries=n_libraries,
            params=params,
        )

        return {
            "report_name": report_info["name"],
            "report_ref": report_info["ref"],
        }

    # ------------------------------------------------------------------
    # Parameter validation
    # ------------------------------------------------------------------

    def _validate_params(self, params):
        required = ["workspace_name", "reads_ref", "read_type",
                    "genome_size", "output_assembly_name"]
        missing = [k for k in required if not params.get(k)]
        if missing:
            raise ValueError(
                "Missing required parameters: {}".format(", ".join(missing))
            )

        if params["read_type"] not in READ_TYPE_FLAGS:
            raise ValueError(
                "read_type '{}' is not valid. Must be one of: {}".format(
                    params["read_type"], ", ".join(READ_TYPE_FLAGS.keys())
                )
            )

        gs = params["genome_size"]
        if not re.match(r"^\d+(\.\d+)?[kmgKMG]?$", str(gs)):
            raise ValueError(
                "genome_size '{}' is not a valid Canu size string "
                "(e.g. '5m', '2.4g', '500k').".format(gs)
            )

    # ------------------------------------------------------------------
    # Object type detection
    # ------------------------------------------------------------------

    def _get_object_type(self, obj_ref):
        """
        Return the KBase type string for a workspace object reference.

        Uses the Workspace service via AssemblyUtil's underlying workspace
        client. Falls back to an empty string if the lookup fails so that
        callers can still attempt to proceed.
        """
        try:
            # AssemblyUtil exposes a ws_client on its internal client object;
            # for robustness we import the workspace client directly.
            from installed_clients.WorkspaceClient import Workspace
            ws = Workspace(self.workspace_url, token=self.token)
            info = ws.get_object_info3({"objects": [{"ref": obj_ref}]})
            # info['infos'][0][2] is the full type string e.g.
            # "KBaseSets.ReadsSet-1.0"
            return info["infos"][0][2]
        except Exception as exc:
            logger.warning(
                "Could not determine object type for ref '%s': %s. "
                "Treating as single reads library.",
                obj_ref, exc
            )
            return ""

    # ------------------------------------------------------------------
    # Reads download — single library
    # ------------------------------------------------------------------

    def _download_reads(self, reads_ref):
        """
        Download a single reads library (SingleEndLibrary or PairedEndLibrary)
        using ReadsUtils.download_reads().

        Returns
        -------
        dict with keys:
            reads_files : list[str]   list containing the single fwd/SE file
            read_count  : str         number of reads (or 'unknown')
            n_libraries : int         always 1
        """
        reads_result = self.ru.download_reads({
            "read_libraries": [reads_ref],
            "interleaved": "false",
        })

        # download_reads returns {"files": {reads_ref: {...}}}
        reads_data = reads_result.get("files", {}).get(reads_ref, {})
        files = reads_data.get("files", {})
        fwd = files.get("fwd") or files.get("fwd_gz")

        if not fwd or not os.path.isfile(fwd):
            raise RuntimeError(
                "Could not locate forward/single reads file for ref '{}'. "
                "ReadsUtils response: {}".format(reads_ref, reads_result)
            )

        logger.info("Downloaded single library to: %s", fwd)
        return {
            "reads_files": [fwd],
            "read_count":  str(reads_data.get("read_count", "unknown")),
            "n_libraries": 1,
        }

    # ------------------------------------------------------------------
    # Reads download — ReadsSet (multiple libraries)
    # ------------------------------------------------------------------

    def _download_reads_set(self, reads_set_ref, workspace_name):
        """
        Expand a KBaseSets.ReadsSet into individual library files and
        download each one.

        ReadsSet items are each a single-end or paired-end library.  For
        long-read assembly we expect single-end libraries; if a paired-end
        library is present we take only the forward file (the reads are
        assumed to be individual long reads, not short-read pairs).

        Returns
        -------
        dict with keys:
            reads_files : list[str]   one file path per library in the set
            read_count  : str         total reads across all libraries
            n_libraries : int         number of libraries in the set
        """
        try:
            from installed_clients.SetAPIServiceClient import SetAPI
            set_api = SetAPI(self.callback_url, token=self.token)
        except Exception:
            # SetAPI not installed — fall back to reading the set object
            # directly from the workspace and enumerating refs manually.
            logger.warning(
                "SetAPI client not available; falling back to direct "
                "workspace lookup for ReadsSet."
            )
            return self._download_reads_set_fallback(reads_set_ref)

        set_data = set_api.get_reads_set_v1({
            "ref": reads_set_ref,
            "include_item_info": 1,
        })

        items = set_data.get("data", {}).get("items", [])
        if not items:
            raise RuntimeError(
                "ReadsSet '{}' contains no items.".format(reads_set_ref)
            )

        logger.info("ReadsSet contains %d item(s); downloading each.", len(items))

        reads_files = []
        total_reads = 0

        for item in items:
            item_ref = item.get("ref")
            label    = item.get("label", item_ref)
            logger.info("  Downloading reads item: %s (%s)", label, item_ref)

            result = self._download_reads(item_ref)
            reads_files.extend(result["reads_files"])

            rc = result.get("read_count", "0")
            try:
                total_reads += int(rc)
            except (ValueError, TypeError):
                pass

        return {
            "reads_files": reads_files,
            "read_count":  str(total_reads) if total_reads else "unknown",
            "n_libraries": len(items),
        }

    def _download_reads_set_fallback(self, reads_set_ref):
        """
        Fallback ReadsSet expander when SetAPI is unavailable.
        Reads the object directly from the workspace and iterates items.
        """
        from installed_clients.WorkspaceClient import Workspace
        ws = Workspace(self.workspace_url, token=self.token)
        obj = ws.get_objects2({"objects": [{"ref": reads_set_ref}]})
        data = obj["data"][0]["data"]
        items = data.get("items", [])

        reads_files = []
        total_reads = 0
        for item in items:
            item_ref = item.get("ref")
            result = self._download_reads(item_ref)
            reads_files.extend(result["reads_files"])
            try:
                total_reads += int(result.get("read_count", 0))
            except (ValueError, TypeError):
                pass

        return {
            "reads_files": reads_files,
            "read_count":  str(total_reads) if total_reads else "unknown",
            "n_libraries": len(items),
        }

    # ------------------------------------------------------------------
    # Merge multiple reads files into one
    # ------------------------------------------------------------------

    @staticmethod
    def _merge_reads_files(reads_files, output_path):
        """
        Concatenate multiple FASTQ/FASTA files (gzipped or plain) into a
        single gzipped output file.  Uses streaming cat/zcat to avoid
        loading everything into memory.

        Parameters
        ----------
        reads_files : list[str]   paths to input reads files
        output_path : str         path for the merged output (will be gzipped)
        """
        import gzip
        import shutil

        with gzip.open(output_path, "wb") as out_fh:
            for fpath in reads_files:
                logger.info("  Merging: %s", fpath)
                if fpath.endswith(".gz"):
                    with gzip.open(fpath, "rb") as in_fh:
                        shutil.copyfileobj(in_fh, out_fh)
                else:
                    with open(fpath, "rb") as in_fh:
                        shutil.copyfileobj(in_fh, out_fh)

        sz_mb = os.path.getsize(output_path) / (1024 * 1024)
        logger.info("Merged reads file: %s (%.1f MB)", output_path, sz_mb)

    # ------------------------------------------------------------------
    # Command construction
    # ------------------------------------------------------------------

    def _build_canu_command(self, params, reads_file, assembly_prefix, work_dir):
        """
        Build the Canu CLI command list.

        The command follows Canu's invocation format:
            canu -p <prefix> -d <work_dir> genomeSize=<gs> [options] <flag> <reads>
        """
        read_type = params["read_type"]
        genome_size = str(params["genome_size"])
        read_flag = READ_TYPE_FLAGS[read_type]

        cmd = [
            "canu",
            "-p", assembly_prefix,
            "-d", work_dir,
            "genomeSize={}".format(genome_size),
            # Disable grid submission; KBase jobs run on a single node
            "useGrid=false",
        ]

        # ---- Optional numeric parameters -----------------------------------
        if params.get("min_read_length"):
            cmd.append("minReadLength={}".format(int(params["min_read_length"])))

        if params.get("corrected_error_rate"):
            cmd.append("correctedErrorRate={:.4f}".format(
                float(params["corrected_error_rate"])
            ))

        if params.get("min_overlap_length"):
            cmd.append("minOverlapLength={}".format(int(params["min_overlap_length"])))

        if params.get("max_input_coverage") and int(params["max_input_coverage"]) > 0:
            cmd.append("maxInputCoverage={}".format(int(params["max_input_coverage"])))

        if params.get("cor_out_coverage"):
            cmd.append("corOutCoverage={}".format(int(params["cor_out_coverage"])))

        if params.get("fast_assembly") and int(params["fast_assembly"]) == 1:
            cmd.append("-fast")

        # ---- Advanced parameters (in KIDL spec, less commonly changed) -----
        if params.get("raw_error_rate"):
            cmd.append("rawErrorRate={:.4f}".format(
                float(params["raw_error_rate"])
            ))

        if params.get("cor_min_coverage") is not None:
            # 0 is a valid value (correct all reads regardless of coverage)
            try:
                val = int(params["cor_min_coverage"])
                cmd.append("corMinCoverage={}".format(val))
            except (ValueError, TypeError):
                pass

        # ---- Read type flag and input file ---------------------------------
        cmd += [read_flag, reads_file]

        return cmd

    # ------------------------------------------------------------------
    # Subprocess execution
    # ------------------------------------------------------------------

    @staticmethod
    def _run_command(cmd):
        """Execute a shell command, streaming stdout/stderr to the logger."""
        logger.info("CMD: %s", " ".join(cmd))
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for line in proc.stdout:
            logger.info(line.rstrip())
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(
                "Canu exited with return code {}. "
                "Check the log above for details.".format(proc.returncode)
            )

    # ------------------------------------------------------------------
    # Output file discovery
    # ------------------------------------------------------------------

    @staticmethod
    def _find_contigs_fasta(work_dir, prefix):
        """
        Locate the primary contigs FASTA produced by Canu.

        Canu names its output '<prefix>.contigs.fasta'. If the assembly
        produced no contigs (e.g. very low coverage), it may only produce
        '<prefix>.unitigs.fasta'. We prefer contigs, fall back to unitigs.
        """
        for suffix in ("contigs.fasta", "unitigs.fasta"):
            candidate = os.path.join(work_dir, "{}.{}".format(prefix, suffix))
            if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
                return candidate

        raise FileNotFoundError(
            "Canu did not produce a contigs or unitigs FASTA in {}. "
            "The assembly may have failed or produced no output. "
            "Check the Canu log for details.".format(work_dir)
        )

    # ------------------------------------------------------------------
    # Assembly statistics parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_assembly_stats(work_dir, prefix):
        """
        Parse the Canu <prefix>.report file for basic assembly statistics.

        Canu v2.2 report format (relevant sections):

            [ASSEMBLY GRAPH STATISTICS]
            contigs              42
            ...

            [CONTIG LENGTHS]
            Total bases          23456789
            ...
            N50                  512345
            ...
            Largest              1234567

        Returns a dict with string values for use in the HTML report.
        Returns an empty dict if the report is absent or unparseable —
        the caller handles this gracefully.
        """
        report_file = os.path.join(work_dir, "{}.report".format(prefix))
        stats = {}

        if not os.path.isfile(report_file):
            logger.warning("Canu report file not found: %s", report_file)
            return stats

        with open(report_file) as fh:
            content = fh.read()

        # Canu v2.2 report patterns — tested against real output.
        # Multiple patterns per key handle minor format variations across
        # Canu versions and assembly modes (hifi vs raw).
        patterns = {
            # Number of contigs (unitigs output as contigs)
            "contigs": [
                r"^contigs\s+(\d+)",
                r"^Contigs\s+(\d+)",
                r"^unitigs\s+(\d+)",
            ],
            # Total assembled bases
            "total_length": [
                r"Total bases\s+([\d,]+)",
                r"Total bases in all contigs\s+([\d,]+)",
                r"bases\s+([\d,]+)",
            ],
            # N50 contig length
            "n50": [
                r"N50\s+([\d,]+)",
                r"n50\s+([\d,]+)",
            ],
            # Longest contig
            "largest_contig": [
                r"Largest\s+([\d,]+)",
                r"largest\s+([\d,]+)",
                r"Max\s+([\d,]+)",
            ],
        }

        for key, pattern_list in patterns.items():
            for pattern in pattern_list:
                m = re.search(pattern, content, re.IGNORECASE | re.MULTILINE)
                if m:
                    stats[key] = m.group(1).replace(",", "")
                    break   # first matching pattern wins

        logger.info("Parsed assembly stats from report: %s", stats)
        return stats

    # ------------------------------------------------------------------
    # Assembly upload
    # ------------------------------------------------------------------

    def _upload_assembly(self, workspace_name, contigs_file,
                            assembly_name, reads_ref, params):
            """
            Upload the contigs FASTA to the KBase workspace as an Assembly object.

            Returns the workspace reference string 'ws_id/obj_id/version'.
            """
            result = self.au.save_assembly_from_fasta({
                "file": {"path": contigs_file},
                "workspace_name": workspace_name,
                "assembly_name": assembly_name,
            })
            return result

    # ------------------------------------------------------------------
    # Report creation
    # ------------------------------------------------------------------

    def _create_report(self, workspace_name, assembly_ref, assembly_name,
                       stats, read_type, read_count, n_libraries, params):
        """
        Build an HTML summary report and save it as a KBaseReport object.
        """
        html_body = self._build_report_html(
            assembly_name=assembly_name,
            assembly_ref=assembly_ref,
            stats=stats,
            read_type=read_type,
            read_count=read_count,
            n_libraries=n_libraries,
            params=params,
        )

        report_dir = os.path.join(self.scratch, "canu_report")
        os.makedirs(report_dir, exist_ok=True)
        report_html_path = os.path.join(report_dir, "report.html")
        with open(report_html_path, "w") as fh:
            fh.write(html_body)

        result = self.kbr.create_extended_report({
            "workspace_name": workspace_name,
            "html_links": [{
                "path": report_dir,
                "name": "report.html",
                "label": "Canu Assembly Report",
            }],
            "direct_html_link_index": 0,
            "objects_created": [{
                "ref": assembly_ref,
                "description": "Canu assembly — {}".format(assembly_name),
            }],
            "report_object_name": "kb_canu_report",
        })

        return {"name": result["name"], "ref": result["ref"]}

    # ------------------------------------------------------------------
    # HTML report builder
    # ------------------------------------------------------------------

    @staticmethod
    def _build_report_html(assembly_name, assembly_ref, stats,
                           read_type, read_count, n_libraries, params):
        """
        Return a polished, self-contained HTML report string.

        Sections:
          1. Run summary  — object name, ref, read type, genome size, read count
          2. Assembly statistics — contigs, total length, N50, largest contig
          3. Canu parameters — all user-supplied parameters
        """

        read_type_display = {
            "nanopore":    "Oxford Nanopore (raw)",
            "pacbio-raw":  "PacBio CLR (pacbio-raw)",
            "pacbio-hifi": "PacBio HiFi / CCS (pacbio-hifi)",
        }.get(read_type, read_type)

        # ---- Assembly statistics table rows --------------------------------
        stat_label_map = [
            ("contigs",        "Number of Contigs"),
            ("total_length",   "Total Assembly Length (bp)"),
            ("n50",            "N50 (bp)"),
            ("largest_contig", "Largest Contig (bp)"),
        ]
        stats_rows = ""
        for key, label in stat_label_map:
            val = stats.get(key, "N/A")
            # Format numbers with thousands separators where possible
            try:
                val = "{:,}".format(int(val))
            except (ValueError, TypeError):
                pass
            stats_rows += "            <tr><td>{}</td><td><b>{}</b></td></tr>\n".format(
                label, val
            )

        # ---- Parameters table rows -----------------------------------------
        param_label_map = {
            "reads_ref":            "Input Reads Reference",
            "read_type":            "Read Type",
            "genome_size":          "Estimated Genome Size",
            "output_assembly_name": "Output Assembly Name",
            "min_read_length":      "Min Read Length (bp)",
            "corrected_error_rate": "Corrected Error Rate",
            "min_overlap_length":   "Min Overlap Length (bp)",
            "max_input_coverage":   "Max Input Coverage",
            "cor_out_coverage":     "Corrected Output Coverage",
            "fast_assembly":        "Fast Assembly Mode",
            "raw_error_rate":       "Raw Error Rate",
            "cor_min_coverage":     "Min Correction Coverage",
        }
        param_rows = ""
        for key, label in param_label_map.items():
            val = params.get(key)
            if val is None or val == "" or val == 0:
                continue
            if key == "fast_assembly":
                val = "Yes" if int(val) == 1 else "No"
            param_rows += "            <tr><td>{}</td><td>{}</td></tr>\n".format(
                label, val
            )

        html = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Canu Assembly Report</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{
      font-family: "Helvetica Neue", Arial, sans-serif;
      font-size: 14px;
      line-height: 1.6;
      color: #2d2d2d;
      background: #f5f7fa;
      margin: 0;
      padding: 0;
    }}
    .page-wrap {{
      max-width: 900px;
      margin: 0 auto;
      padding: 24px 32px 48px;
    }}
    /* ---- Header banner ---- */
    .banner {{
      background: linear-gradient(135deg, #1a4a7a 0%, #2c7bb6 100%);
      color: #fff;
      border-radius: 6px;
      padding: 20px 28px;
      margin-bottom: 28px;
      display: flex;
      align-items: center;
      gap: 20px;
    }}
    .banner-icon {{
      font-size: 42px;
      line-height: 1;
    }}
    .banner h1 {{
      margin: 0;
      font-size: 22px;
      font-weight: 700;
      letter-spacing: 0.3px;
    }}
    .banner .subtitle {{
      margin: 4px 0 0;
      font-size: 13px;
      opacity: 0.85;
    }}
    /* ---- Cards ---- */
    .card {{
      background: #fff;
      border: 1px solid #dce3ec;
      border-radius: 6px;
      margin-bottom: 24px;
      overflow: hidden;
    }}
    .card-header {{
      background: #eef3f9;
      border-bottom: 1px solid #dce3ec;
      padding: 10px 18px;
      font-size: 13px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.6px;
      color: #1a4a7a;
    }}
    .card-body {{
      padding: 0;
    }}
    /* ---- Summary boxes ---- */
    .summary-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 0;
    }}
    .summary-box {{
      padding: 16px 20px;
      border-right: 1px solid #dce3ec;
      border-bottom: 1px solid #dce3ec;
    }}
    .summary-box:last-child {{ border-right: none; }}
    .summary-box .label {{
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: #888;
      margin-bottom: 4px;
    }}
    .summary-box .value {{
      font-size: 20px;
      font-weight: 700;
      color: #1a4a7a;
    }}
    .summary-box .sub {{
      font-size: 11px;
      color: #aaa;
      margin-top: 2px;
    }}
    /* ---- Tables ---- */
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    thead th {{
      background: #f0f4f8;
      padding: 9px 16px;
      text-align: left;
      font-weight: 600;
      color: #444;
      border-bottom: 2px solid #dce3ec;
    }}
    tbody td {{
      padding: 8px 16px;
      border-bottom: 1px solid #eef0f3;
      color: #333;
    }}
    tbody tr:last-child td {{ border-bottom: none; }}
    tbody tr:hover {{ background: #f7f9fc; }}
    /* ---- Footer ---- */
    .footer {{
      text-align: center;
      font-size: 11px;
      color: #aaa;
      margin-top: 32px;
    }}
    .footer a {{ color: #2c7bb6; text-decoration: none; }}
  </style>
</head>
<body>
<div class="page-wrap">

  <!-- Banner -->
  <div class="banner">
    <div class="banner-icon">&#x1F9EC;</div>
    <div>
      <h1>Canu Assembly Report</h1>
      <div class="subtitle">
        Assembly: <strong>{assembly_name}</strong> &nbsp;&middot;&nbsp;
        Read type: <strong>{read_type_display}</strong>
      </div>
    </div>
  </div>

  <!-- Run Summary card -->
  <div class="card">
    <div class="card-header">Run Summary</div>
    <div class="card-body">
      <div class="summary-grid">
        <div class="summary-box">
          <div class="label">Genome Size (est.)</div>
          <div class="value">{genome_size}</div>
          <div class="sub">user-specified</div>
        </div>
        <div class="summary-box">
          <div class="label">Input Reads</div>
          <div class="value">{read_count}</div>
          <div class="sub">{n_libraries_label}</div>
        </div>
        <div class="summary-box">
          <div class="label">Contigs Assembled</div>
          <div class="value">{n_contigs}</div>
          <div class="sub">primary contigs</div>
        </div>
        <div class="summary-box">
          <div class="label">Assembly N50</div>
          <div class="value">{n50_display}</div>
          <div class="sub">bp</div>
        </div>
      </div>
    </div>
  </div>

  <!-- Assembly Statistics card -->
  <div class="card">
    <div class="card-header">Assembly Statistics</div>
    <div class="card-body">
      <table>
        <thead><tr><th>Metric</th><th>Value</th></tr></thead>
        <tbody>
{stats_rows}        </tbody>
      </table>
    </div>
  </div>

  <!-- Output Object card -->
  <div class="card">
    <div class="card-header">Output Object</div>
    <div class="card-body">
      <table>
        <thead><tr><th>Field</th><th>Value</th></tr></thead>
        <tbody>
          <tr><td>Assembly Object Name</td><td><b>{assembly_name}</b></td></tr>
          <tr><td>Workspace Reference</td><td><code>{assembly_ref}</code></td></tr>
          <tr><td>Read Type</td><td>{read_type_display}</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- Parameters card -->
  <div class="card">
    <div class="card-header">Parameters Used</div>
    <div class="card-body">
      <table>
        <thead><tr><th>Parameter</th><th>Value</th></tr></thead>
        <tbody>
{param_rows}        </tbody>
      </table>
    </div>
  </div>

  <div class="footer">
    Generated by <strong>kb_canu</strong> &mdash; Canu v2.2 wrapper for
    <a href="https://kbase.us" target="_blank">KBase</a> &nbsp;&middot;&nbsp;
    <a href="https://doi.org/10.1101/gr.215087.116" target="_blank">
      Koren <i>et al.</i> 2017, <i>Genome Research</i>
    </a>
  </div>

</div>
</body>
</html>""".format(
            assembly_name=assembly_name,
            assembly_ref=assembly_ref,
            read_type_display=read_type_display,
            genome_size=params.get("genome_size", "N/A"),
            read_count=read_count,
            n_libraries_label="{} librar{}".format(
                n_libraries, "y" if n_libraries == 1 else "ies"
            ),
            n_contigs=stats.get("contigs", "N/A"),
            n50_display=stats.get("n50", "N/A"),
            stats_rows=stats_rows,
            param_rows=param_rows,
        )
        return html
