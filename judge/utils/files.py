import secrets


def generate_secure_filename(original_filename, prefix=None):
    """
    Generate a secure filename with a random suffix to prevent URL guessing.

    Args:
        original_filename: The original uploaded filename
        prefix: Optional prefix (e.g., 'user_1', 'problem_code')

    Returns:
        A filename like 'myfile_a1b2c3d4.png' or 'user_1_myfile_a1b2c3d4.png'
    """
    if "." in original_filename:
        base_name, extension = original_filename.rsplit(".", 1)
        extension = "." + extension.lower()
    else:
        base_name = original_filename
        extension = ""

    base_name = "".join(
        c for c in base_name if c.isalnum() or c in (" ", "-", "_")
    ).rstrip()
    if not base_name:
        base_name = "file"

    random_suffix = secrets.token_hex(4)

    if prefix:
        return f"{prefix}_{random_suffix}{extension}"
    return f"{base_name}_{random_suffix}{extension}"
