import os
import csv
import logging
import shutil
import asyncio
from datetime import datetime, timedelta
from typing import List, Optional
import pandas as pd

# Constants
HEIZUNGSDATEN_CSV = os.path.join("csv log", "heizungsdaten.csv")
ARCHIVE_DIR = os.path.join("csv log", "archiv")
MAX_ARCHIVE_SIZE_MB = 50
ROTATION_THRESHOLD_DAYS = 14
KEEP_DAYS = 7 # We archive everything older than this (effectively days 14 down to 8)

class CSVRotator:
    def __init__(self, csv_path: str = HEIZUNGSDATEN_CSV, archive_dir: str = ARCHIVE_DIR):
        self.csv_path = csv_path
        self.archive_dir = archive_dir
        self.last_check_date: Optional[datetime.date] = None

    def ensure_dirs(self):
        """Creates the archive directory if it doesn't exist."""
        if not os.path.exists(self.archive_dir):
            os.makedirs(self.archive_dir)
            logging.info(f"Archive directory created: {self.archive_dir}")

    def get_archive_filename(self, oldest_date_str: str) -> str:
        """
        Generates a filename based on the oldest date in the archive chunk.
        Format: heizungslog + oldest_date.csv
        """
        clean_date = oldest_date_str.replace(":", "-").replace(" ", "_")
        return f"heizungslog {clean_date}.csv"

    def rotate(self):
        """
        Performs the rotation logic:
        1. Checks if oldest data is >= 14 days old.
        2. If so, moves data from day 14 to day 8 into an archive.
        3. Manages archive file sizes (max 50MB).
        """
        try:
            if not os.path.exists(self.csv_path):
                logging.debug(f"CSV for rotation not found: {self.csv_path}")
                return

            # Load the CSV
            # Using pandas for easier date manipulation, but for huge files we might need chunked reading.
            # 50MB is small enough for pandas in memory on most systems.
            df = pd.read_csv(self.csv_path, parse_dates=["Zeitstempel"])
            if df.empty:
                return

            now = datetime.now()
            oldest_timestamp = df["Zeitstempel"].min()
            
            # Check if oldest data is at least 14 days old
            if (now - oldest_timestamp).days < ROTATION_THRESHOLD_DAYS:
                logging.debug("Oldest data is not yet 14 days old. Skipping rotation.")
                return

            logging.info(f"Starting CSV rotation. Oldest record: {oldest_timestamp}")

            # Define the window to archive: Everything older than KEEP_DAYS (7 days)
            # This covers "Day 14 to 8" implicitly as long as we run daily.
            cutoff_date = now - timedelta(days=KEEP_DAYS)
            
            archive_df = df[df["Zeitstempel"] < cutoff_date].copy()
            remaining_df = df[df["Zeitstempel"] >= cutoff_date].copy()

            if archive_df.empty:
                logging.info("No data found to archive within the window.")
                return

            # Perform the archival
            self.ensure_dirs()
            self._write_to_archive(archive_df)

            # Update the main CSV (Atomic replace)
            temp_csv = self.csv_path + ".tmp"
            remaining_df.to_csv(temp_csv, index=False)
            shutil.move(temp_csv, self.csv_path)
            
            logging.info(f"Rotation complete. Archived {len(archive_df)} rows. Main CSV now has {len(remaining_df)} rows.")

        except Exception as e:
            logging.error(f"Error during CSV rotation: {e}", exc_info=True)

    def _write_to_archive(self, df: pd.DataFrame):
        """Writes the dataframe to an archive file, respecting size limits."""
        # Get oldest date for naming
        oldest_date_str = df["Zeitstempel"].min().strftime("%Y-%m-%d")
        archive_name = self.get_archive_filename(oldest_date_str)
        archive_path = os.path.join(self.archive_dir, archive_name)

        # Basic size check and increment naming if needed
        counter = 1
        final_path = archive_path
        while os.path.exists(final_path):
            size_mb = os.path.getsize(final_path) / (1024 * 1024)
            if size_mb < MAX_ARCHIVE_SIZE_MB:
                # Append to current archive
                df.to_csv(final_path, mode='a', header=False, index=False)
                logging.info(f"Appended archived data to {final_path}")
                return
            else:
                # Create new numbered archive
                basename = os.path.splitext(archive_name)[0]
                final_path = os.path.join(self.archive_dir, f"{basename}_{counter}.csv")
                counter += 1

        # Write fresh archive
        df.to_csv(final_path, index=False)
        logging.info(f"Created new archive file: {final_path}")

    async def run_daily(self):
        """Background loop to check for rotation once a day."""
        while True:
            now = datetime.now()
            if self.last_check_date != now.date():
                logging.info("Triggering daily CSV rotation check...")
                # Run the blocking rotation logic in a separate thread
                await asyncio.to_thread(self.rotate)
                self.last_check_date = now.date()
            
            # Sleep for an hour before checking again if the day changed
            await asyncio.sleep(3600)

if __name__ == "__main__":
    # Setup basic logging for standalone run
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    rotator = CSVRotator()
    rotator.rotate()
