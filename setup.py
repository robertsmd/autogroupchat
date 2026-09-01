from setuptools import setup, find_packages

VERSION = '0.3.0'
DESCRIPTION = 'a package for automation of group chat creation'
LONG_DESCRIPTION = 'Scrape data from Google Sheets to get info on how to create groups, then create them using the GroupMe or Signal backend.'

with open('requirements.txt') as f:
    requirements = f.read().splitlines()

# Setting up
setup(
    # the name must match the folder name 'verysimplemodule'
    name="autogroupchat",
    version=VERSION,
    author="Michael R",
    author_email="<s41l8hu2@duck.com",
    description=DESCRIPTION,
    long_description=LONG_DESCRIPTION,
    packages=find_packages(),
    install_requires=requirements,
    extras_require={
        'signal-gcs': ['google-cloud-storage'],
        'dev': ['pytest'],
    },

    keywords=['GroupMe', 'Signal', 'Google Sheets',
              "Group Chat", "Messaging", "Automation"],
    classifiers=[
        "Programming Language :: Python :: 3",
    ]
)
