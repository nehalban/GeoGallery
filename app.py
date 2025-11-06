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
    photo_extensions = {'.jpg', '.jpeg', '.png', '.tiff', '.tif', '.raw', '.cr2', '.nef', '.arw', '.heic'}
    
    def __init__(self, source_folder, google_api_key=None):
        self.source_folder = Path(source_folder)
        self.google_api_key = google_api_key or API_KEY if API_KEY != "YOUR_GOOGLE_MAPS_API_KEY" else None
        
        # Lazy caches - only populated as needed
        self.location_cache = {}  # Cache for GPS coordinates
        self.date_cache = {}  # Cache for dates
        self.geocoding_cache = {}  # Cache for reverse geocoding results
        
    def _extract_exif_data(self, image_path):
        """Extract both GPS coordinates and date from EXIF data in a single pass."""
        try:
            with open(image_path, 'rb') as f:
                # Extract all relevant tags in one pass
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
        """Get GPS coordinates with lazy caching."""
        if image_path not in self.location_cache:
            coordinates, date_taken = self._extract_exif_data(image_path)
            self.location_cache[image_path] = coordinates
            self.date_cache[image_path] = date_taken
        return self.location_cache[image_path]
    
    def get_date_lazy(self, image_path):
        """Get date with lazy caching."""
        if image_path not in self.date_cache:
            coordinates, date_taken = self._extract_exif_data(image_path)
            self.location_cache[image_path] = coordinates
            self.date_cache[image_path] = date_taken
        return self.date_cache[image_path]
    
    def _convert_to_degrees(self, value):
        """Convert GPS coordinates from DMS to decimal degrees."""
        d, m, s = value.values
        return float(d) + float(m)/60.0 + float(s)/3600.0
    
    @staticmethod
    def are_locations_same(coord1, coord2, tolerance=0.01):
        """Check if two GPS coordinates are at the same location within tolerance."""
        if coord1 is None or coord2 is None:
            return coord1 == coord2  # Both None or one is None
        
        lat_diff = abs(coord1[0] - coord2[0])
        lon_diff = abs(coord1[1] - coord2[1])
        
        return lat_diff <= tolerance and lon_diff <= tolerance
    
    def find_location_group_end(self, photos, start_index):
        """Use binary search technique to find the end of a location group."""
        if start_index >= len(photos):
            return start_index
        
        start_location = self.get_location_lazy(photos[start_index])
        
        # If no location data, treat as single-item group
        if start_location is None:
            return start_index + 1
        
        # Exponential search to find upper bound
        left = start_index + 1
        right = len(photos)
        step = 8  # Start with 8th photo
        
        current = start_index + step
        while current < len(photos):
            current_location = self.get_location_lazy(photos[current])
            
            if not self.are_locations_same(start_location, current_location):
                right = current
                break
            
            left = current
            step *= 2
            current = start_index + step
        
        # Binary search between left and right
        while left < right:
            mid = (left + right) // 2
            mid_location = self.get_location_lazy(photos[mid])
            
            if self.are_locations_same(start_location, mid_location):
                left = mid + 1
            else:
                right = mid
        
        return left
    
    def get_location_name(self, coordinates):
        """Generate a location name from coordinates (fallback method)."""
        if coordinates is None:
            return "no_location"
        
        lat, lon = coordinates
        # Create a simple location identifier
        lat_dir = "N" if lat >= 0 else "S"
        lon_dir = "E" if lon >= 0 else "W"
        
        return f"{abs(lat):.4f}{lat_dir}_{abs(lon):.4f}{lon_dir}"
    
    def get_location_name_from_google(self, coordinates):
        """Get location name from Google Maps API with caching."""
        if coordinates is None:
            return "no_location"
        
        # Check cache first
        coord_key = f"{coordinates[0]:.4f},{coordinates[1]:.4f}"
        if coord_key in self.geocoding_cache:
            return self.geocoding_cache[coord_key]
        
        try:
            import requests
            import time
            
            url = "https://maps.googleapis.com/maps/api/geocode/json"
            params = {
                'latlng': f"{coordinates[0]},{coordinates[1]}",
                'key': self.google_api_key
            }
            
            response = requests.get(url, params=params)
            time.sleep(0.1)  # Rate limiting
            
            if response.status_code == 200:
                data = response.json()
                if data['results']:
                    # Get city/locality from results
                    address_components = data['results'][0]['address_components']
                    for component in address_components:
                        if 'locality' in component['types']:
                            location_name = component['long_name'].replace(' ', '_')
                            self.geocoding_cache[coord_key] = location_name
                            return location_name
                    
                    # Fallback to first formatted address component
                    location_name = data['results'][0]['address_components'][0]['long_name'].replace(' ', '_')
                    self.geocoding_cache[coord_key] = location_name
                    return location_name
        
        except Exception as e:
            logger.warning(f"Error with Google API: {e}")
        
        # Fallback to coordinate-based name
        fallback_name = self.get_location_name(coordinates)
        self.geocoding_cache[coord_key] = fallback_name
        return fallback_name
    
    def get_best_location_name(self, coordinates):
        """Get the best available location name."""
        if self.google_api_key:
            return self.get_location_name_from_google(coordinates)
        else:
            return self.get_location_name(coordinates)
    
    def process_photos(self):
        """Main processing function to sort photos by location."""
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
        """Create subfolders and move photos based on location and date."""
        
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
    """Main function to run the photo sorter."""
    
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
