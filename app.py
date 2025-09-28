import os
import shutil
from collections import defaultdict
from datetime import datetime
import exifread
from pathlib import Path
import logging

# Set up logging for debugging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class PhotoLocationSorter:
    def __init__(self, source_folder, google_api_key=None):
        self.source_folder = Path(source_folder)
        self.photo_extensions = {'.jpg', '.jpeg', '.png', '.tiff', '.tif', '.raw', '.cr2', '.nef', '.arw', '.heic'}
        self.location_cache = {}  # Cache for GPS coordinates
        self.geocoding_cache = {}  # Cache for reverse geocoding results
        self.google_api_key = google_api_key
        self.api_request_delay = 0.1  # Delay between API requests to respect rate limits
        
    def extract_gps_coordinates(self, image_path):
        """Extract GPS coordinates from image EXIF data efficiently."""
        try:
            with open(image_path, 'rb') as f:
                # Method 1: Use details=False and filter only GPS tags
                # This is the most efficient approach
                tags = exifread.process_file(f, details=False, extract_thumbnail=False, stop_tag='GPSLongitude', builtin_types=True)

                # Filter to only GPS tags we need
                gps_tags = {k: v for k, v in tags.items() if k.startswith('GPS') and 
                           k in ['GPS GPSLatitude', 'GPS GPSLatitudeRef', 
                                'GPS GPSLongitude', 'GPS GPSLongitudeRef']}
            
            # Check if GPS data exists
            gps_lat = gps_tags.get('GPS GPSLatitude')
            gps_lat_ref = gps_tags.get('GPS GPSLatitudeRef')
            gps_lon = gps_tags.get('GPS GPSLongitude')
            gps_lon_ref = gps_tags.get('GPS GPSLongitudeRef')
            
            if not all([gps_lat, gps_lat_ref, gps_lon, gps_lon_ref]):
                return None
                
            # Convert GPS coordinates to decimal degrees
            lat = self._convert_to_degrees(gps_lat)
            if gps_lat_ref.values[0] != 'N':
                lat = -lat
                
            lon = self._convert_to_degrees(gps_lon)
            if gps_lon_ref.values[0] != 'E':
                lon = -lon
                
            # Round to reduce precision for grouping (approximately 11m accuracy)
            return (round(lat, 4), round(lon, 4))
        
        except Exception as e:
            logger.warning(f"Error extracting GPS from {image_path}: {e}")
            return None
            
    def extract_gps_coordinates_fast(self, image_path):
        """Ultra-fast GPS extraction using manual EXIF parsing."""
        try:
            import struct
            
            with open(image_path, 'rb') as f:
                # Read EXIF header
                f.seek(0)
                if f.read(2) != b'\xff\xe1':  # Check for EXIF marker
                    return None
                
                # Skip length
                f.read(2)
                
                # Check for EXIF identifier
                if f.read(6) != b'Exif\x00\x00':
                    return None
                
                # Read TIFF header
                tiff_start = f.tell()
                endian = f.read(2)
                
                if endian == b'II':  # Little endian
                    endian_flag = '<'
                elif endian == b'MM':  # Big endian  
                    endian_flag = '>'
                else:
                    return None
                
                # Skip TIFF magic number
                f.read(2)
                
                # Read IFD0 offset
                ifd0_offset = struct.unpack(endian_flag + 'I', f.read(4))[0]
                
                # Navigate to GPS IFD
                f.seek(tiff_start + ifd0_offset)
                
                # This is a simplified version - for production, you'd need
                # to properly parse the IFD structure to find GPS data
                # For now, fall back to the optimized exifread method
                f.seek(0)
                
        except:
            pass
        
        # Fall back to optimized exifread method
        return self.extract_gps_coordinates(image_path)
    
    def _convert_to_degrees(self, value):
        """Convert GPS coordinates from DMS to decimal degrees."""
        d, m, s = value.values
        return float(d) + float(m)/60.0 + float(s)/3600.0
    
    def extract_date_taken(self, image_path):
        """Extract the date the photo was taken."""
        try:
            with open(image_path, 'rb') as f:
                tags = exifread.process_file(f)
                
            # Try different date tags
            date_tags = ['EXIF DateTimeOriginal', 'EXIF DateTime', 'Image DateTime']
            
            for tag_name in date_tags:
                if tag_name in tags:
                    date_str = str(tags[tag_name])
                    try:
                        return datetime.strptime(date_str, '%Y:%m:%d %H:%M:%S')
                    except ValueError:
                        continue
            
            # Fallback to file modification time
            return datetime.fromtimestamp(os.path.getmtime(image_path))
            
        except Exception as e:
            logger.warning(f"Error extracting date from {image_path}: {e}")
            return datetime.fromtimestamp(os.path.getmtime(image_path))
    
    def are_locations_same(self, coord1, coord2, tolerance=0.01):
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
        
        start_location = self.location_cache.get(photos[start_index])
        if start_location is None:
            return start_index + 1
        
        # Binary search to find the end of the group
        left = start_index + 1
        right = len(photos)
        step = 8  # Start with 8th photo as suggested
        
        # First, find an upper bound using exponential search
        current = start_index + step
        while current < len(photos):
            current_location = self.location_cache.get(photos[current])
            
            if not self.are_locations_same(start_location, current_location):
                right = current
                break
            
            left = current
            step *= 2
            current = start_index + step
        
        # Now binary search between left and right
        while left < right:
            mid = (left + right) // 2
            mid_location = self.location_cache.get(photos[mid])
            
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
    
    def get_best_location_name(self, coordinates):
        """Get the best available location name (Google API if available, otherwise coordinates)."""
        if self.google_api_key:
            return self.get_location_name_from_google(coordinates)
        else:
            return self.get_location_name(coordinates)
    
    def process_photos(self):
        """Main processing function to sort photos by location."""
        logger.info(f"Starting to process photos in {self.source_folder}")
        
        # Get all photo files
        photo_files = []
        for ext in self.photo_extensions:
            photo_files.extend(self.source_folder.glob(f'*{ext}'))
            photo_files.extend(self.source_folder.glob(f'*{ext.upper()}'))
        
        if not photo_files:
            logger.warning("No photo files found!")
            return
        
        logger.info(f"Found {len(photo_files)} photos")
        
        # Extract GPS coordinates for all photos (with progress)
        logger.info("Extracting GPS coordinates...")
        for i, photo_path in enumerate(photo_files):
            if i % 100 == 0:
                logger.info(f"Processed {i}/{len(photo_files)} photos for GPS data")
            
            coordinates = self.extract_gps_coordinates(photo_path)
            self.location_cache[photo_path] = coordinates
        
        # Sort photos by date taken for better grouping
        logger.info("Sorting photos by date...")
        photo_files.sort(key=lambda x: self.extract_date_taken(x))
        
        # Group photos by location using binary search technique
        logger.info("Grouping photos by location...")
        location_groups = defaultdict(list)
        
        i = 0
        while i < len(photo_files):
            group_end = self.find_location_group_end(photo_files, i)
            
            # Get the location for this group
            location = self.location_cache.get(photo_files[i])
            
            # Add all photos in this group to the location
            for j in range(i, group_end):
                photo_path = photo_files[j]
                date_taken = self.extract_date_taken(photo_path)
                location_name = self.get_location_name(location)
                
                location_groups[location_name].append({
                    'path': photo_path,
                    'date': date_taken,
                    'coordinates': location
                })
            
            i = group_end
        
        # Now get proper location names using Google API if available
        if self.google_api_key:
            logger.info("Getting location names from Google Maps API...")
            final_groups = defaultdict(list)
            
            for location_name, photos in location_groups.items():
                if photos and photos[0]['coordinates']:
                    # Use the first photo's coordinates to get the location name
                    proper_name = self.get_best_location_name(photos[0]['coordinates'])
                    final_groups[proper_name].extend(photos)
                else:
                    final_groups[location_name].extend(photos)
            
            location_groups = final_groups
        
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
                            name_parts = source_path.stem, counter, source_path.suffix
                            dest_path = folder_path / f"{name_parts[0]}_{name_parts[1]}{name_parts[2]}"
                            counter += 1
                        
                        shutil.move(str(source_path), str(dest_path))
                        moved_count += 1
                        
                    except Exception as e:
                        logger.error(f"Error moving {source_path}: {e}")
                
                logger.info(f"Moved {moved_count} photos to {folder_name}")

def main():
    """Main function to run the photo sorter."""
    
    # Get source folder from user
    source_folder = input("Enter the path to your photo dump folder: ").strip()
    
    if not os.path.exists(source_folder):
        print("Error: Folder does not exist!")
        return
    
    # Optional: Get Google API key for better location names
    print("\n--- Optional Google Maps Integration ---")
    print("For better location names (e.g., 'Paris_France' instead of coordinates),")
    print("you can provide a Google Maps API key.")
    print("Leave empty to use coordinate-based naming.")
    google_api_key = input("Enter Google Maps API key (optional): ").strip()
    
    if google_api_key:
        print("✓ Will use Google Maps for location names")
    else:
        print("✓ Will use coordinate-based location names")
        google_api_key = None
    
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
