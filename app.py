import os
import shutil
from collections import defaultdict
from datetime import datetime
import exifread
from pathlib import Path
import logging

API_KEY = "YOUR_GOOGLE_MAPS_API_KEY"  # Replace with your actual API key

# Set up logging for debugging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class PhotoLocationSorter:
    """Sorts and organizes photos by date and location.

    This class:
    - Extracts EXIF GPS and date metadata (with lazy caching)
    - Groups photos by approximate location using a search-based grouping strategy
    - Optionally resolves human-readable place names via Google Geocoding API
    - Creates folders per date and location, then moves photos accordingly
    """
    photo_extensions = {'.jpg', '.jpeg', '.png', '.tiff', '.tif', '.raw', '.cr2', '.nef', '.arw', '.heic'}
    
    def __init__(self, source_folder, google_api_key=None):
        """Initialize the sorter.

        Args:
            source_folder (str | Path): Path to the folder containing photos.
            google_api_key (str | None): Optional API key for Google Geocoding.
                If not provided, falls back to coordinate-based names.
        """
        self.source_folder = Path(source_folder)
        self.google_api_key = google_api_key or (API_KEY if API_KEY != "YOUR_GOOGLE_MAPS_API_KEY" else None)
        
        # Lazy caches - only populated as needed
        self.location_cache = {}  # Cache for GPS coordinates
        self.date_cache = {}  # Cache for dates
        self.geocoding_cache = {}  # Cache for reverse geocoding results
        
    def _extract_exif_data(self, image_path):
        """Extract both GPS coordinates and date from EXIF data in a single pass.

        Uses exifread with minimal details to reduce overhead. Coordinates are
        rounded to 4 decimals to stabilize grouping (~11m).

        Args:
            image_path (Path): Path to the image.

        Returns:
            tuple[tuple[float, float] | None, datetime]: (rounded (lat, lon) or None, date_taken).
                Falls back to file modification time if EXIF date is missing.
        """
        try:
            with open(image_path, 'rb') as f:
                # Extract all relevant tags in one pass for performance
                tags = exifread.process_file(
                    f, 
                    details=False, 
                    extract_thumbnail=False
                )
            
            # Extract GPS coordinates
            coordinates = None
            gps_lat = tags.get('GPS GPSLatitude')
            gps_lat_ref = tags.get('GPS GPSLatitudeRef')
            gps_lon = tags.get('GPS GPSLongitude')
            gps_lon_ref = tags.get('GPS GPSLongitudeRef')
            
            if all([gps_lat, gps_lat_ref, gps_lon, gps_lon_ref]):
                try:
                    # Convert GPS coordinates to decimal degrees
                    lat = self._convert_to_degrees(gps_lat)
                    if gps_lat_ref.values[0] != 'N':
                        lat = -lat
                        
                    lon = self._convert_to_degrees(gps_lon)
                    if gps_lon_ref.values[0] != 'E':
                        lon = -lon
                        
                    # Round to reduce precision for grouping (approximately 11m accuracy)
                    coordinates = (round(lat, 4), round(lon, 4))
                except Exception as e:
                    logger.warning(f"Error converting GPS coordinates for {image_path.name}: {e}")
            
            # Extract date
            date_taken = None
            date_tags = ['EXIF DateTimeOriginal', 'EXIF DateTime', 'Image DateTime']
            
            for tag_name in date_tags:
                if tag_name in tags:
                    date_str = str(tags[tag_name])
                    try:
                        date_taken = datetime.strptime(date_str, '%Y:%m:%d %H:%M:%S')
                        break
                    except ValueError:
                        continue
            
            # Fallback to file modification time if no EXIF date
            if date_taken is None:
                date_taken = datetime.fromtimestamp(os.path.getmtime(image_path))
            
            return coordinates, date_taken
            
        except Exception as e:
            logger.warning(f"Error reading EXIF from {image_path.name}: {e}")
            # Return fallback values
            return None, datetime.fromtimestamp(os.path.getmtime(image_path))
    
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
    
    def _convert_to_degrees(self, value):
        """Convert GPS coordinates from DMS to decimal degrees.

        Args:
            value: EXIF rational triplet for degrees, minutes, seconds.

        Returns:
            float: Decimal degrees.
        """
        d, m, s = value.values
        return float(d) + float(m)/60.0 + float(s)/3600.0
    
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
    

    def get_location_name_from_google(self, coordinates, prefer_locality=True):
        """Resolve a human-readable name using Google Geocoding API with caching.

        Prefers city/locality names when available; falls back to formatted address.
        Results are cached by rounded coordinate string (~11m).

        Args:
            coordinates (tuple[float, float]): Rounded (lat, lon).
            prefer_locality (bool): If True, prefer city/locality when present.

        Returns:
            str | None: Resolved location name or None if unavailable/errored.
        """
        if not coordinates or not all(isinstance(c, (int, float)) for c in coordinates):
            logging.warning("Invalid or missing coordinates provided.")
            return None
        
        coord_key = f"{coordinates[0]:.4f},{coordinates[1]:.4f}"
        
        if coord_key in self.geocoding_cache:
            return self.geocoding_cache[coord_key]
        
        params = {
            'latlng': f"{coordinates[0]},{coordinates[1]}",
            'key': self.google_api_key
        }

        try:
            import requests
            response = requests.get("https://maps.googleapis.com/maps/api/geocode/json", params=params)
            
            if response.status_code != 200:
                logging.warning(
                    f"HTTP Error {response.status_code} for {coord_key}: {response.text}"
                )
                return None

            data = response.json()

            if data['status'] != 'OK':
                logging.info(
                    f"API Error for {coord_key}. Status: {data['status']}"
                )
                self.geocoding_cache[coord_key] = None
                return None
            
            if not data.get('results'):
                logging.info(f"API status OK but no results for {coord_key}")
                self.geocoding_cache[coord_key] = None
                return None
                
            first_result = data['results'][0]
            location_name = None

            if prefer_locality:
                address_components = first_result.get('address_components', [])
                for component in address_components:
                    if 'locality' in component['types']:
                        location_name = component['long_name']
                        break
            
            if location_name is None:
                location_name = first_result.get('formatted_address')

            self.geocoding_cache[coord_key] = location_name
            return location_name

        except requests.exceptions.RequestException as e:
            logging.error(f"Geocoding network error for {coord_key}: {e}")
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
    
    def process_photos(self):
        """Sort photos by date and group by location, then move into folders.

        Flow:
        - Gather candidate photo files by supported extensions
        - Sort by modification time (proxy for date when EXIF missing)
        - Group by location using search-based grouping
        - Resolve location names (API if enabled)
        - Create date_location folders and move photos
        """
        logger.info(f"Starting to process photos in {self.source_folder}")
        
        # Get all photo files (sorted by modification time as proxy for date)
        photo_files = sorted(
            [p for p in self.source_folder.iterdir() 
             if p.is_file() and p.suffix.lower() in self.photo_extensions],
            key=lambda x: os.path.getmtime(x)
        )
        
        if not photo_files:
            logger.warning("No photo files found!")
            return
        
        num = len(photo_files)
        logger.info(f"Found {num} photos")
        
        # Group photos by location using binary search technique
        logger.info("Grouping photos by location...")
        location_groups = defaultdict(list)
        
        i = 0
        processed = 0
        while i < len(photo_files):
            group_end = self.find_location_group_end(photo_files, i)
            
            # Get the location for this group
            location = self.get_location_lazy(photo_files[i])
            
            # Get location name once per group (with Google API if available)
            location_name = self.get_best_location_name(location)
            
            # Add all photos in this group to the location
            for j in range(i, group_end):
                photo_path = photo_files[j]
                date_taken = self.get_date_lazy(photo_path)
                
                location_groups[location_name].append({
                    'path': photo_path,
                    'date': date_taken,
                    'coordinates': location
                })
            
            processed += (group_end - i)
            if processed % 100 == 0 or processed == num:
                logger.info(f"Processed {processed}/{num} photos")
            
            i = group_end
        
        # Create folders and move photos
        logger.info(f"Found {len(location_groups)} location groups")
        self.create_folders_and_move_photos(location_groups)
    
    def create_folders_and_move_photos(self, location_groups):
        """Create subfolders and move photos based on location and date.

        For each location group:
        - Partition photos by date (YYYY-MM-DD)
        - Create a folder named `{date}_{location_name}`
        - Move photos into the corresponding folder, handling duplicate filenames
        """
        for location_name, photos in location_groups.items():
            if not photos:
                continue
            
            # Group photos by date for this location
            date_groups = defaultdict(list)
            for photo_info in photos:
                date_key = photo_info['date'].strftime('%Y-%m-%d')
                date_groups[date_key].append(photo_info)
            
            # Create folders and move photos for each date
            for date_key, date_photos in date_groups.items():
                folder_name = f"{date_key}_{location_name}"
                folder_path = self.source_folder / folder_name
                
                # Create folder if it doesn't exist
                folder_path.mkdir(exist_ok=True)
                
                # Move photos to the folder
                moved_count = 0
                for photo_info in date_photos:
                    try:
                        source_path = photo_info['path']
                        dest_path = folder_path / source_path.name
                        
                        # Handle duplicate filenames
                        counter = 1
                        while dest_path.exists():
                            dest_path = folder_path / f"{source_path.stem}_{counter}{source_path.suffix}"
                            counter += 1
                        
                        shutil.move(str(source_path), str(dest_path))
                        moved_count += 1
                        
                    except Exception as e:
                        logger.error(f"Error moving {source_path.name}: {e}")
                
                logger.info(f"Moved {moved_count} photos to {folder_name}")

def main():
    """CLI entry point to execute the photo sorter interactively."""
    
    # Get source folder from user
    source_folder = input("Enter the path to your photo dump folder: ").strip()
    
    if not os.path.exists(source_folder):
        print("Error: Folder does not exist!")
        return
    
    # Optional: Get Google API key for better location names
    use_google = input("Use Google Maps API for location names? (y/n): ").strip().lower()
    google_api_key = None
    
    if use_google == 'y':
        google_api_key = input("Enter Google Maps API key: ").strip()
        if google_api_key:
            print("✓ Will use Google Maps for location names")
        else:
            print("✓ No API key provided, using coordinate-based names")
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
