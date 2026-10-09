import os
import shutil
import time
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple
import exifread
from pathlib import Path
import logging

API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "YOUR_GOOGLE_MAPS_API_KEY")

# Set up logging for debugging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Simple rate limiter for external API calls (issue #14)
_LAST_API_CALL_TS: Optional[float] = None
_API_MIN_INTERVAL_SEC = 0.2  # ~5 QPS max

class PhotoLocationSorter:
    """Sorts and organizes photos by date and location.

    This class:
    - Extracts EXIF GPS and date metadata (with lazy caching)
    - Groups photos by approximate location using a search-based grouping strategy
    - Optionally resolves human-readable place names via Google Geocoding API
    - Creates folders per date and location, then moves photos accordingly
    """

    # Normalize extensions to lowercase (issue #8)
    photo_extensions = {ext.lower() for ext in {'.jpg', '.jpeg', '.png', '.tiff', '.tif', '.raw', '.cr2', '.nef', '.arw', '.heic'}}

    def __init__(self, source_folder: Path | str, google_api_key: Optional[str] = None) -> None:
        """Initialize the sorter.

        Args:
            source_folder (str | Path): Path to the folder containing photos.
            google_api_key (str | None): Optional API key for Google Geocoding.
                If not provided, falls back to coordinate-based names.
        """
        self.source_folder: Path = Path(source_folder)
        env_key = API_KEY if API_KEY and API_KEY != "YOUR_GOOGLE_MAPS_API_KEY" else None
        self.google_api_key: Optional[str] = google_api_key or env_key

        # Lazy caches - only populated as needed
        self.location_cache: Dict[Path, Optional[Tuple[float, float]]] = {}
        self.date_cache: Dict[Path, datetime] = {}
        self.geocoding_cache: Dict[str, Optional[str]] = {}
        
    def _extract_exif_data(self, image_path: Path) -> Tuple[Optional[Tuple[float, float]], datetime]:
        """Extract both GPS coordinates and date from EXIF data in a single pass.

        Uses exifread with minimal details to reduce overhead. Coordinates are
        rounded to 4 decimals to stabilize grouping (~11m).

        Robust error handling (issue #1), optimized parsing flags (issue #2),
        and DMS conversion correctness (issue #3).
        """
        try:
            with open(image_path, 'rb') as f:
                tags = exifread.process_file(
                    f,
                    details=False,
                    extract_thumbnail=False,
                    builtin_types=True,
                )
        except Exception as e:
            logger.warning(f"Error reading EXIF from {image_path.name}: {e}")
            return None, datetime.fromtimestamp(os.path.getmtime(image_path))

        coordinates: Optional[Tuple[float, float]] = None
        try:
            gps_lat = tags.get('GPS GPSLatitude')
            gps_lat_ref = tags.get('GPS GPSLatitudeRef')
            gps_lon = tags.get('GPS GPSLongitude')
            gps_lon_ref = tags.get('GPS GPSLongitudeRef')

            if gps_lat and gps_lat_ref and gps_lon and gps_lon_ref:
                lat = self._convert_to_degrees(gps_lat)
                lat_ref = str(getattr(gps_lat_ref, 'values', [gps_lat_ref])[0])
                if lat_ref.upper() == 'S':
                    lat = -lat

                lon = self._convert_to_degrees(gps_lon)
                lon_ref = str(getattr(gps_lon_ref, 'values', [gps_lon_ref])[0])
                if lon_ref.upper() == 'W':
                    lon = -lon

                coordinates = (round(lat, 4), round(lon, 4))  # issue #4
        except Exception as e:
            logger.warning(f"Error converting GPS for {image_path.name}: {e}")

        # Date extraction with graceful fallback (issue #1)
        date_taken: Optional[datetime] = None
        for tag_name in ('EXIF DateTimeOriginal', 'EXIF DateTime', 'Image DateTime'):
            try:
                if tag_name in tags:
                    date_str = str(tags[tag_name])
                    date_taken = datetime.strptime(date_str, '%Y:%m:%d %H:%M:%S')
                    break
            except Exception:
                continue
        if date_taken is None:
            date_taken = datetime.fromtimestamp(os.path.getmtime(image_path))

        return coordinates, date_taken
    
    def get_location_lazy(self, image_path):
        """Return cached GPS coordinates or extract on demand.

        Args:
            image_path (Path): Image path.

        Returns:
            tuple[float, float] | None: Rounded (lat, lon) if available; else None.
        """
        if image_path not in self.location_cache:
            coordinates, date_taken = self._extract_exif_data(image_path)
            self.location_cache[image_path] = coordinates
            self.date_cache[image_path] = date_taken
        return self.location_cache[image_path]
    
    def get_date_lazy(self, image_path):
        """Return cached date or extract on demand.

        Args:
            image_path (Path): Image path.

        Returns:
            datetime: Date/time the photo was taken or file mtime fallback.
        """
        if image_path not in self.date_cache:
            coordinates, date_taken = self._extract_exif_data(image_path)
            self.location_cache[image_path] = coordinates
            self.date_cache[image_path] = date_taken
        return self.date_cache[image_path]
    
    def _convert_to_degrees(self, value) -> float:
        """Convert GPS coordinates from DMS to decimal degrees (issue #3).

        Handles rational tuples and ensures floats.
        """
        try:
            vals = getattr(value, 'values', value)
            d, m, s = vals
            d = float(d)
            m = float(m)
            s = float(s)
            return d + (m / 60.0) + (s / 3600.0)
        except Exception as e:
            raise ValueError(f"Invalid DMS value: {value} ({e})")
    
    @staticmethod
    def are_locations_same(coord1, coord2, tolerance=0.01):
        """Heuristically determine if two coordinates represent the same place.

        Compares each component within the given tolerance. If either is None,
        equality requires both to be None (no-location bucket).

        Args:
            coord1 (tuple[float, float] | None): First coordinate.
            coord2 (tuple[float, float] | None): Second coordinate.
            tolerance (float): Allowed difference in degrees per axis.

        Returns:
            bool: True if considered the same location.
        """
        if coord1 is None or coord2 is None:
            return coord1 == coord2  # Both None or one is None
        
        lat_diff = abs(coord1[0] - coord2[0])
        lon_diff = abs(coord1[1] - coord2[1])
        
        return lat_diff <= tolerance and lon_diff <= tolerance
    
    def find_location_group_end(self, photos, start_index):
        """Find the exclusive end index of a contiguous location group.

        Strategy:
        - Start from start_index and perform an exponential search (step=8, doubling)
          to quickly find an upper bound where location changes.
        - Use binary search within the discovered range to find the first differing index.
        - Returns the exclusive end index of the group starting at start_index.

        Args:
            photos (list[Path]): Sorted list of photo paths.
            start_index (int): Index where the group begins.

        Returns:
            int: Exclusive end index for the group.
        """
        if start_index >= len(photos):
            return start_index
        
        start_location = self.get_location_lazy(photos[start_index])
        
        # If no location data, treat as single-item group
        if start_location is None:
            return start_index + 1
        
        # Exponential search to find upper bound
        left = start_index + 1
        right = len(photos)
        step = 8  # Start with 8th photo for faster skipping over long same-location runs
        
        current = start_index + step
        while current < len(photos):
            current_location = self.get_location_lazy(photos[current])
            
            if not self.are_locations_same(start_location, current_location):
                right = current
                break
            
            left = current
            step *= 2
            current = start_index + step
        
        # Binary search between left and right to locate first differing index
        while left < right:
            mid = (left + right) // 2
            mid_location = self.get_location_lazy(photos[mid])
            
            if self.are_locations_same(start_location, mid_location):
                left = mid + 1
            else:
                right = mid
        
        return left
    
    def get_location_name(self, coordinates):
        """Generate a simple coordinate-based name.

        Args:
            coordinates (tuple[float, float] | None): Rounded (lat, lon).

        Returns:
            str: e.g., '12.3456N_98.7654E' or 'no_location' if None.
        """
        if coordinates is None:
            return "no_location"
        
        lat, lon = coordinates
        lat_dir = "N" if lat >= 0 else "S"
        lon_dir = "E" if lon >= 0 else "W"
        
        return f"{abs(lat):.4f}{lat_dir}_{abs(lon):.4f}{lon_dir}"
    

    def get_location_name_from_google(self, coordinates: Tuple[float, float], prefer_locality: bool = True) -> Optional[str]:
        """Resolve a human-readable name using Google Geocoding API with caching and rate limiting.

        - Caches results per rounded coordinate (issue #5)
        - Reads API key from env/ctor and degrades gracefully (issue #6)
        - Applies simple rate limiting (issue #14)
        """
        if not self.google_api_key:
            return None
        if not coordinates or not all(isinstance(c, (int, float)) for c in coordinates):
            logger.warning("Invalid or missing coordinates provided.")
            return None

        coord_key = f"{coordinates[0]:.4f},{coordinates[1]:.4f}"
        if coord_key in self.geocoding_cache:
            return self.geocoding_cache[coord_key]

        # Rate limiting
        global _LAST_API_CALL_TS
        if _LAST_API_CALL_TS is not None:
            elapsed = time.time() - _LAST_API_CALL_TS
            if elapsed < _API_MIN_INTERVAL_SEC:
                time.sleep(_API_MIN_INTERVAL_SEC - elapsed)

        params = {
            'latlng': f"{coordinates[0]},{coordinates[1]}",
            'key': self.google_api_key
        }

        try:
            import requests
            response = requests.get("https://maps.googleapis.com/maps/api/geocode/json", params=params, timeout=10)
            _LAST_API_CALL_TS = time.time()

            if response.status_code != 200:
                logger.warning(f"HTTP {response.status_code} for {coord_key}: {response.text[:200]}")
                self.geocoding_cache[coord_key] = None
                return None

            data = response.json()
            if data.get('status') != 'OK':
                logger.info(f"API status for {coord_key}: {data.get('status')}")
                self.geocoding_cache[coord_key] = None
                return None

            results = data.get('results') or []
            if not results:
                self.geocoding_cache[coord_key] = None
                return None

            first = results[0]
            location_name: Optional[str] = None
            if prefer_locality:
                for comp in first.get('address_components', []):
                    if 'locality' in comp.get('types', []):
                        location_name = comp.get('long_name')
                        break
            if not location_name:
                location_name = first.get('formatted_address')

            self.geocoding_cache[coord_key] = location_name
            return location_name
        except Exception as e:
            logger.error(f"Geocoding error for {coord_key}: {e}")
            return None

    
    def get_best_location_name(self, coordinates):
        """Return the best available location name.

        Uses the Google Geocoding API if configured; otherwise falls back to
        coordinate-based naming.
        """
        if self.google_api_key:
            return self.get_location_name_from_google(coordinates)
        else:
            return self.get_location_name(coordinates)
    
    def process_photos(self) -> None:
        """Sort photos by date and group by location, then move into folders.

        Implements date-first ordering with an adaptive quicksort-like algorithm (issue #11):
        - Build a list of candidate files
        - Extract dates lazily and sort with a key that is already nearly sorted
        - Python's Timsort already optimizes for runs; we further minimize EXIF reads via caching
        """
        logger.info(f"Starting to process photos in {self.source_folder}")

        # Gather candidate photo files (issue #8 + #15 Pathlib)
        photo_files: List[Path] = [
            p for p in self.source_folder.iterdir()
            if p.is_file() and p.suffix.lower() in self.photo_extensions
        ]
        if not photo_files:
            logger.warning("No photo files found!")
            return

        # Sort primarily by date (lazy reads). Timsort handles nearly-sorted data efficiently.
        # We prefill date cache for a light pass to avoid repeated EXIF openings in sort key.
        for p in photo_files:
            _ = self.get_date_lazy(p)
        photo_files.sort(key=lambda x: self.date_cache.get(x, datetime.fromtimestamp(os.path.getmtime(x))))

        num = len(photo_files)
        logger.info(f"Found {num} photos; grouping by location...")

        location_groups: Dict[str, List[Dict[str, object]]] = defaultdict(list)
        i = 0
        processed = 0
        while i < len(photo_files):
            group_end = self.find_location_group_end(photo_files, i)

            location = self.get_location_lazy(photo_files[i])
            location_name = self.get_best_location_name(location)
            if not location_name:
                location_name = self.get_location_name(location)

            for j in range(i, group_end):
                photo_path = photo_files[j]
                date_taken = self.get_date_lazy(photo_path)
                location_groups[location_name].append({'path': photo_path, 'date': date_taken, 'coordinates': location})

            processed += (group_end - i)
            if processed % 100 == 0 or processed == num:
                logger.info(f"Processed {processed}/{num} photos")

            i = group_end

        logger.info(f"Found {len(location_groups)} location groups")
        self.create_folders_and_move_photos(location_groups)
    
    def create_folders_and_move_photos(self, location_groups: Dict[str, List[Dict[str, object]]]) -> None:
        """Create subfolders and move photos based on location and date.

        - Handles duplicate filenames safely (issue #7)
        - Retries moves to mitigate Windows/OneDrive locks (issue #13)
        """
        for location_name, photos in location_groups.items():
            if not photos:
                continue

            date_groups: Dict[str, List[Dict[str, object]]] = defaultdict(list)
            for photo_info in photos:
                date_key = photo_info['date'].strftime('%Y-%m-%d')
                date_groups[date_key].append(photo_info)

            for date_key, date_photos in date_groups.items():
                safe_loc = location_name.replace(os.sep, '_') if isinstance(location_name, str) else 'no_location'
                folder_name = f"{date_key}_{safe_loc}"
                folder_path = self.source_folder / folder_name
                folder_path.mkdir(exist_ok=True)

                moved_count = 0
                for photo_info in date_photos:
                    source_path: Path = photo_info['path']  # type: ignore[index]

                    # Build a non-colliding destination path (issue #7)
                    dest_path = folder_path / source_path.name
                    counter = 1
                    while dest_path.exists():
                        dest_path = folder_path / f"{source_path.stem}_{counter}{source_path.suffix}"
                        counter += 1

                    # Attempt move with retries (issue #13)
                    attempts = 0
                    while True:
                        try:
                            shutil.move(str(source_path), str(dest_path))
                            moved_count += 1
                            break
                        except Exception as e:
                            attempts += 1
                            if attempts >= 3:
                                logger.error(f"Error moving {source_path.name} after {attempts} attempts: {e}")
                                break
                            time.sleep(0.3 * attempts)

                logger.info(f"Moved {moved_count} photos to {folder_name}")

def main() -> None:
    """CLI entry point to execute the photo sorter interactively."""

    source_folder = input("Enter the path to your photo dump folder: ").strip()

    if not Path(source_folder).exists():
        print("Error: Folder does not exist!")
        return

    use_google = input("Use Google Maps API for location names? (y/n): ").strip().lower()
    google_api_key: Optional[str] = None

    if use_google == 'y':
        # Prefer env var; allow override by input
        entered = input("Enter Google Maps API key (leave blank to use env): ").strip()
        google_api_key = entered or os.getenv('GOOGLE_MAPS_API_KEY')
        if google_api_key:
            print("✓ Will use Google Maps for location names")
        else:
            print("✓ No API key available, using coordinate-based names")
    else:
        print("✓ Will use coordinate-based location names")

    print(f"\nStarting photo sorting process for: {source_folder}")
    print("This may take a while for large photo collections...")

    try:
        sorter = PhotoLocationSorter(source_folder, google_api_key)
        sorter.process_photos()
        print("\nPhoto sorting completed successfully!")

    except KeyboardInterrupt:
        print("\nProcess interrupted by user.")
    except Exception as e:
        print(f"\nError during processing: {e}")
        logger.exception("Detailed error information:")

if __name__ == "__main__":
    main()
