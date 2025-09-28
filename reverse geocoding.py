import requests
import json

def get_location_name(latitude, longitude, api_key):
    """
    Takes latitude, longitude, and an API key, and returns the location name.
    """
    # The base URL for the Google Maps Geocoding API
    base_url = "https://maps.googleapis.com/maps/api/geocode/json"

    # The parameters to send with the request
    params = {
        "latlng": f"{latitude},{longitude}",
        "key": api_key
    }

    try:
        # Make the HTTP GET request
        response = requests.get(base_url, params=params)
        
        # Check if the request was successful (status code 200)
        if response.status_code == 200:
            data = response.json()
            
            # Check if Google returned any results for the coordinates
            if data['status'] == 'OK':
                # The most complete address is usually the first result
                formatted_address = data['results'][0]['formatted_address']
                return formatted_address
            else:
                return f"Google Maps API could not find a location. Status: {data['status']}"
        else:
            return f"HTTP Request failed with status code: {response.status_code}"

    except requests.exceptions.RequestException as e:
        return f"An error occurred: {e}"

# --- Main Program ---
if __name__ == "__main__":
    # Replace with your actual API key
    MY_API_KEY = "YOUR_GOOGLE_MAPS_API_KEY" 

    # Example coordinates for the Eiffel Tower
    lat = 48.8584
    lon = 2.2945

    # Get the location name
    location_name = get_location_name(lat, lon, MY_API_KEY)
    
    # Print the result
    print(f"Coordinates: ({lat}, {lon})")
    print(f"Location: {location_name}")

    # Another example: The Taj Mahal
    lat_taj = 27.1751
    lon_taj = 78.0421
    location_name_taj = get_location_name(lat_taj, lon_taj, MY_API_KEY)
    print(f"\nCoordinates: ({lat_taj}, {lon_taj})")
    print(f"Location: {location_name_taj}")
