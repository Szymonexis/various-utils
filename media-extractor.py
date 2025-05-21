import os
import shutil
import mimetypes
from pathlib import Path


def is_media_file(file_path):
    """
    Determine if a file is a media file (image, video, or audio)
    """
    # Initialize mimetypes
    mimetypes.init()

    # Get the MIME type of the file
    mime_type, _ = mimetypes.guess_type(file_path)

    # If MIME type couldn't be determined, try by extension
    if mime_type is None:
        # Common media file extensions
        media_extensions = [
            # Images
            '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp', '.svg', '.heic', '.raw',
            # Videos
            '.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.mpg', '.mpeg',
            # Audio
            '.mp3', '.wav', '.ogg', '.flac', '.aac', '.m4a', '.wma'
        ]
        return file_path.lower().endswith(tuple(media_extensions))

    # Check if the MIME type starts with image/, video/, or audio/
    return mime_type and (mime_type.startswith('image/') or
                          mime_type.startswith('video/') or
                          mime_type.startswith('audio/'))


def find_media_files(source_dir):
    """
    Recursively find all media files in the given directory
    """
    media_files = []
    total_files = 0

    print("Scanning files...")

    # Walk through all files and directories
    for root, _, files in os.walk(source_dir):
        for file in files:
            total_files += 1
            file_path = os.path.join(root, file)

            # Show progress every 100 files
            if total_files % 100 == 0:
                print(
                    f"\rScanned {total_files} files, found {len(media_files)} media files...", end="")

            if is_media_file(file_path):
                media_files.append(file_path)

    print(
        f"\rScanned {total_files} files, found {len(media_files)} media files.")
    return media_files


def copy_files_flat(files, destination_dir):
    """
    Copy files to destination directory with a flat structure
    """
    # Create destination directory if it doesn't exist
    os.makedirs(destination_dir, exist_ok=True)

    copied_count = 0
    skipped_count = 0

    # Calculate total size for progress reporting
    total_files = len(files)

    for i, file_path in enumerate(files):
        try:
            # Get the file name
            file_name = os.path.basename(file_path)

            # Create destination file path
            dest_file = os.path.join(destination_dir, file_name)

            # Handle filename conflicts by adding a number if needed
            counter = 1
            while os.path.exists(dest_file):
                name, ext = os.path.splitext(file_name)
                dest_file = os.path.join(
                    destination_dir, f"{name}_{counter}{ext}")
                counter += 1

            # Copy the file with metadata (creation time, etc.)
            shutil.copy2(file_path, dest_file)
            copied_count += 1

            # Print progress
            print(
                f"\rCopying: {copied_count}/{total_files} files copied...", end="")

        except Exception as e:
            print(f"\nError copying {file_path}: {str(e)}")
            skipped_count += 1

    print(
        f"\nDone! Copied {copied_count} files, skipped {skipped_count} files due to errors.")
    return copied_count


def main():
    """
    Main function to handle the media file extraction process
    """
    print("Media File Extractor")
    print("-------------------")

    # Get source directory from user
    source_dir = input("Enter the source directory path: ").strip()

    # Validate source directory
    if not os.path.isdir(source_dir):
        print(f"Error: The path '{source_dir}' is not a valid directory.")
        return

    # Get destination directory from user or use default
    default_dest = os.path.join(os.path.dirname(source_dir), "extracted_media")
    dest_input = input(
        f"Enter the destination directory path (default: {default_dest}): ").strip()
    dest_dir = dest_input if dest_input else default_dest

    print(f"\nSearching for media files in '{source_dir}'...")
    media_files = find_media_files(source_dir)

    if not media_files:
        print("No media files found.")
        return

    print(f"Found {len(media_files)} media files.")
    print(f"Copying files to '{dest_dir}'...\n")

    copied = copy_files_flat(media_files, dest_dir)

    print(
        f"\nProcess complete! Successfully copied {copied} media files to '{dest_dir}'.")


if __name__ == "__main__":
    main()
