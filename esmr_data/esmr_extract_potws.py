import numpy as np
import pandas as pd
import os
import warnings
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse
import zipfile
import io
import json
import re
import sys
import time
import logging
import yaml
import argparse
from esmr_data import esmr

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(logging.StreamHandler())


def find_zip_url(soup):
    """Return the direct URL of the 'Zipped CSV' resource on the dataset page, or None."""
    # Primary: schema.org JSON-LD embedded in the page
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except ValueError:
            continue
        distributions = data.get("distribution", []) if isinstance(data, dict) else []
        for dist in distributions:
            content_url = dist.get("contentUrl", "")
            if "Zipped CSV" in dist.get("name", "") and content_url.lower().endswith(".zip"):
                return content_url
    # Fallback: the download button carries the resource name only in aria-label
    link = soup.find("a", attrs={"aria-label": lambda v: v and "Zipped CSV" in v})
    if link and link.get("href", "").lower().endswith(".zip"):
        return link["href"]
    return None


def download_and_unzip(url, extract_to="."):
    # Ensure the extract_to directory exists
    if not os.path.exists(extract_to):
        os.makedirs(extract_to)

    response = requests.get(url)
    if response.status_code != 200:
        logger.info(f"Failed to retrieve the URL: {response.status_code}")
        return None

    final_url = find_zip_url(BeautifulSoup(response.content, "html.parser"))
    if not final_url:
        logger.info("Zipped CSV link not found")
        return None
    logger.info(f"Found Zipped CSV link: {final_url}")

    fname = os.path.join(extract_to, urlparse(final_url).path.split("/")[-1])

    # Download the zipped file
    try:
        with requests.get(final_url, stream=True, verify=True) as r:
            r.raise_for_status()
            with open(fname, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
    except requests.exceptions.RequestException as e:
        # The zip is served from a redirect to cloud storage, which some networks block
        logger.info(f"Zip download failed: {e}")
        if os.path.exists(fname):
            os.remove(fname)
        return None
    logger.info(f"File downloaded successfully: {fname}")

    # Unzip the file
    with zipfile.ZipFile(fname, "r") as z:
        z.extractall(extract_to)
        csv_names = [n for n in z.namelist() if n.lower().endswith(".csv")]
    logger.info("File downloaded and extracted successfully")
    if not csv_names:
        logger.info("No CSV found in the downloaded zip")
        return None
    expected = os.path.basename(fname).replace(".zip", ".csv")
    csv_name = expected if expected in csv_names else csv_names[0]
    return os.path.join(extract_to, csv_name)


def download_from_datastore(url, filter_conditions, extract_to=".", years=None):
    """Build a filtered CSV from the portal's per-year datastore dumps.

    The dumps are served by the portal itself (no redirect to cloud storage) and are
    filtered server-side to the facilities/parameters named in filter_conditions.
    """
    parsed = urlparse(url)
    api = f"{parsed.scheme}://{parsed.netloc}"
    package_id = parsed.path.rstrip("/").split("/")[-1]

    r = requests.get(
        f"{api}/api/3/action/package_show", params={"id": package_id}, timeout=60
    )
    r.raise_for_status()
    resources = [
        res
        for res in r.json()["result"]["resources"]
        if res.get("datastore_active") and re.match(r"^\d{4} eSMR", res.get("name", ""))
    ]
    if years:
        resources = [res for res in resources if res["name"][:4] in years]
    if not resources:
        logger.info("No per-year datastore resources found")
        return None

    facilities, parameters, place_types = set(), set(), set()
    for facility, params in filter_conditions.items():
        facilities.add(facility.replace("_", " "))
        for parameter, conditions in params.items():
            if parameter == "station_id":
                continue
            parameters.add(parameter.replace("_", " "))
            place_types.add(conditions["location_place_type"])
    filters = json.dumps(
        {
            "facility_name": sorted(facilities),
            "parameter": sorted(parameters),
            "location_place_type": sorted(place_types),
        }
    )

    frames = []
    for res in sorted(resources, key=lambda x: x["name"]):
        logger.info(f"Downloading datastore dump: {res['name']}")
        started = time.time()
        dump = requests.get(
            f"{api}/datastore/dump/{res['id']}",
            params={"format": "csv", "filters": filters},
            timeout=(30, 900),
        )
        dump.raise_for_status()
        frame = pd.read_csv(
            io.BytesIO(dump.content),
            dtype=str,
            keep_default_na=False,
            na_values=[""],
        )
        logger.info(f"  {len(frame)} rows in {time.time() - started:.0f}s")
        frames.append(frame)

    df = pd.concat(frames, ignore_index=True)
    if df.empty:
        logger.info("Datastore returned no rows for the configured facilities")
        return None
    df = df.drop(columns=["_id"], errors="ignore")

    # Keep the large intermediate (and its .pkl cache) out of the output folder itself
    work_dir = os.path.join(extract_to, "datastore_filtered")
    os.makedirs(work_dir, exist_ok=True)
    fname = os.path.join(work_dir, "esmr_datastore_filtered.csv")
    df.to_csv(fname, index=False)
    # read_data_csv caches a .pkl beside the csv; drop any stale one
    pkl = os.path.splitext(fname)[0] + ".pkl"
    if os.path.exists(pkl):
        os.remove(pkl)
    logger.info(f"Wrote {len(df)} rows from {len(frames)} yearly dumps: {fname}")
    return fname


def process_csv(esmr_file, filter_conditions, extract_to="."):

    df = esmr.read_data_csv(esmr_file)
    data = esmr.ESMR(df)
    logger.info(f"Number of WWTP facilities : {len(data.get_facility_names())}")

    # Extract facility names from filter_conditions
    facility_names = filter_conditions.keys()

    dfmap = {}
    for facility_name in facility_names:
        logger.info(f"Processing facility: {facility_name}")
        for parameter, conditions in filter_conditions[facility_name].items():
            if parameter == "station_id":
                continue
            location_place_type = conditions.pop("location_place_type")
            dff = df[
                (df.facility_name == facility_name.replace("_", " "))
                & (df.location_place_type == location_place_type)
                & (df.parameter == parameter.replace("_", " "))
            ]
            fname = facility_name.replace(" ", "_").replace("/", "_")
            pname = parameter.replace(" ", "_")
            dfmap[f"{fname}_{pname}"] = dff

    plotmap = {}
    for facility_name, parameters in filter_conditions.items():
        for parameter, conditions in parameters.items():
            if parameter == "station_id":
                station_id = conditions
                continue
            key = f"{facility_name}_{parameter}"
            dfk = dfmap[key]
            filter_condition = pd.Series([True] * len(dfk), index=dfk.index)
            for column, condition in conditions.items():
                if condition == "notna":
                    filter_condition &= dfk[column].notna()
                else:
                    filter_condition &= dfk[column] == condition
            filtered_indices = filter_condition[filter_condition.index.isin(dfk.index)]
            dfr = extract_result(dfk, filter_condition, key)
            metadata = get_columns_unique_vals(dfk)
            plotmap[key] = (dfr, metadata)
            syear = str(dfr.index.min().year)
            eyear = str(dfr.index.max().year)
            if parameter == "Electrical_Conductivity_@_25_Deg._C":
                parameter = "ec"
            if parameter == "Flow":
                parameter = "flow"
            if parameter == "Temperature":
                parameter = "temp"
            output_file = f"esmr_{station_id}_{parameter}_{syear}_{eyear}.csv"
            fname = os.path.join(extract_to, output_file)
            write_out_data(dfr, metadata, fname)

    return plotmap


def get_columns_unique_vals(df):
    col_vals = {}
    for col in df.columns:
        if col not in [
            "result",
            "sampling_datetime",
            "analysis_datetime",
            "report_name",
            "smr_document_id",
        ]:
            col_vals[col] = df[col].unique().tolist()
    return col_vals


def write_out_data(dfk, metadata, fname):
    with open(fname, "w", newline="") as f:
        for ckey, cval in metadata.items():
            cval = str(cval).encode("ascii", "replace").decode("ascii")
            f.write(f"# {ckey}: {cval}\n")
        dfk.to_csv(f)


def extract_result(df, filter_condition, key, resample_condition="D"):
    dfk = df[filter_condition]
    if key.endswith("Flow"):
        dfr = dfk[["result"]].resample(resample_condition).sum()
    else:
        dfr = dfk[["result"]].resample(resample_condition).mean()
    return dfr


def plot_data(plotmap):
    import hvplot.pandas  # Import hvplot here to avoid dependency in the main script

    for key, (dfr, metadata) in plotmap.items():
        plot_type = "step" if "Electrical_Conductivity" in key else "line"
        if plot_type == "step":
            plot = dfr.hvplot.step()
        else:
            plot = dfr.hvplot()
        hvplot.save(plot.opts(title=key), f"{key}.png")


def main():
    parser = argparse.ArgumentParser(description="Process ESMR data")
    parser.add_argument(
        "--url", type=str, required=False, default=None, help="URL to download the zipped CSV file"
    )
    parser.add_argument(
        "--config", type=str, required=True, help="Path to the YAML configuration file"
    )
    parser.add_argument(
        "--extract_to",
        type=str,
        default=".",
        help="Directory to write output CSVs",
    )
    parser.add_argument(
        "--csv-file",
        type=str,
        default=None,
        help="Path to an existing ESMR CSV file; skips download and unzip entirely",
    )
    parser.add_argument(
        "--source",
        choices=["auto", "zip", "datastore"],
        default="auto",
        help="Where to download from: the zipped CSV, the portal datastore API, "
        "or auto (zip first, datastore if the zip cannot be downloaded)",
    )
    parser.add_argument(
        "--years",
        type=str,
        default=None,
        help="Comma-separated years to fetch with the datastore source (default: all)",
    )
    # add option to skip download and unzip step
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Skip downloading and unzipping the file",
    )
    # add option to plot the data
    parser.add_argument(
        "--plot",
        action="store_true",
        help="Plot the data",
    )
    args = parser.parse_args()

    with open(args.config, "r") as f:
        filter_conditions = yaml.safe_load(f)
    # Ensure the extract_to directory exists
    if not os.path.exists(args.extract_to):
        os.makedirs(args.extract_to)
    if args.csv_file:
        esmr_file = args.csv_file
    elif not args.skip_download:
        esmr_file = None
        if args.source in ("auto", "zip"):
            esmr_file = download_and_unzip(args.url, args.extract_to)
        if not esmr_file and args.source in ("auto", "datastore"):
            logger.info("Using the portal datastore API")
            years = args.years.replace(" ", "").split(",") if args.years else None
            esmr_file = download_from_datastore(
                args.url, filter_conditions, args.extract_to, years
            )
    else:
        # find local file starging with esmr ending with .csv
        esmr_file = None
        # Find the latest ESMR file in the directory
        esmr_files = [
            os.path.join(args.extract_to, file)
            for file in os.listdir(args.extract_to)
            if file.startswith("esmr") and file.endswith(".csv")
        ]
        if esmr_files:
            esmr_file = max(esmr_files, key=os.path.getctime)
    if esmr_file:
        logger.info(f"Processing ESMR file: {esmr_file}")
        plotmap = process_csv(esmr_file, filter_conditions, args.extract_to)
        if args.plot:
            plot_data(plotmap)
    else:
        logger.error("No ESMR file found")
        sys.exit(1)


if __name__ == "__main__":
    main()
