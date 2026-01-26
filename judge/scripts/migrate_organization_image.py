# Download organization images from "logo_override_image" and upload to organization_images folder to use "organization_image"
# In folder online_judge, run python3 manage.py shell < judge/scripts/migrate_organization_image.py

import os
import requests
from urllib.parse import urlparse
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.conf import settings
from django.db import transaction
from judge.models import Organization


def is_valid_image_url(url):
    try:
        parsed_url = urlparse(url)
        _, ext = os.path.splitext(parsed_url.path)
        return ext.lower() in [".jpg", ".jpeg", ".png", ".gif", ".svg"]
    except Exception:
        return False


def download_image(url):
    response = requests.get(url)
    response.raise_for_status()
    return ContentFile(response.content)


def organization_image_path(organization, filename):
    tail = filename.split(".")[-1]
    new_filename = f"organization_{organization.id}.{tail}"
    return os.path.join(settings.DMOJ_ORGANIZATION_IMAGE_ROOT, new_filename)


@transaction.atomic
def migrate_images():
    print("Start")
    organizations = Organization.objects.all()
    for org in organizations:
        if org.logo_override_image:
            if is_valid_image_url(org.logo_override_image):
                try:
                    # Download the image
                    image_content = download_image(org.logo_override_image)
                    # Determine the file extension
                    file_ext = org.logo_override_image.split(".")[-1]
                    filename = f"organization_{org.id}.{file_ext}"
                    # Save the image to the new location
                    new_path = organization_image_path(org, filename)
                    saved_path = default_storage.save(new_path, image_content)
                    # Update the organization_image field
                    org.organization_image = saved_path
                    org.save()
                    print(f"Image for organization {org.id} migrated successfully.")
                except Exception as e:
                    print(f"Failed to migrate image for organization {org.id}: {e}")
            else:
                print(
                    f"Invalid image URL for organization {org.id}: {org.logo_override_image}"
                )
    print("Finish")


migrate_images()
