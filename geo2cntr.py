import re
from geopy.geocoders import Nominatim
from collections import defaultdict
import os
import sys
# connection to the internet is needed
def Rprint(text):
	print("\033[37;41m"+text+"\033[0m")

if len(sys.argv) > 1:
    SourceFile = sys.argv[1]
    try:
        with open(SourceFile, 'r', encoding='utf-8') as f:
            print(f"Processing: {SourceFile}")
    except FileNotFoundError:
        Rprint(f"Error: File {SourceFile} not found.")
        exit()
else:
    Rprint("Use: python geo2cntr.py <file_path>")
    exit()

geolocator = Nominatim(user_agent="gpx_splitter")
pattern = re.compile(r'<wpt.*?lat="([^"]+)" lon="([^"]+)".*?>(.*?)</wpt>', re.DOTALL)
countries_data = defaultdict(list)

with open(SourceFile, "r", encoding="utf-8") as ReadSourceFile:
    GPXorg = ReadSourceFile.read()

def GetCountryCode(lat, lon):
    try:
        location = geolocator.reverse((lat, lon), language='en', timeout=10)
        country_code = location.raw['address'].get('country_code', '').upper()
        return country_code
    except Exception as e:
        print(f"Error in reverse geocoding: {e}")
        return None

# creating dictionary
for match in pattern.finditer(GPXorg):
    lat = float(match.group(1))
    latR = round(lat,6)
    lon = float(match.group(2))
    lonR = round(lon,6)
    wpt_block =  f'<wpt lat="{latR}" lon="{lonR}">{match.group(3)}</wpt>'
    print(f'\r{latR} {lonR}', end='', flush=True)
    country_code = GetCountryCode(lat, lon)

    if country_code:
        countries_data[country_code].append(wpt_block)
print("")

# creating files
for country_code, wpts in countries_data.items():
    GPXfile = f"{country_code}.gpx"

    # existing file
    if os.path.exists(GPXfile):
        with open(GPXfile, "r", encoding="utf-8") as f:
            file_text = f.read()
        file_text = file_text.rstrip("\n")  # delete last line
    else:
        # header of new file
        file_text = f"<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>\n<gpx version='1.1' creator='AI+M444' xmlns='http://www.topografix.com/GPX/1/1' xmlns:osmand='https://osmand.net' xmlns:xsi='http://www.w3.org/2001/XMLSchema-instance' xsi:schemaLocation='http://www.topografix.com/GPX/1/1 http://www.topografix.com/GPX/1/1/gpx.xsd'>\n<metadata>\n<name>{country_code}</name>\n<author>\n<name>AI+M444</name>\n<link href='https://mariush444.github.io/Osmand-tools/'/>\n</author>\n</metadata>\n"

    # wpt into file
    file_text += "\n".join(wpts) + "\n"

    # end of the file
    file_text += "</gpx>"

    # write file to disk
    with open(GPXfile, "w", encoding="utf-8") as f:
        f.write(file_text)
    print(f"{GPXfile} was created.")

print("End of process")
