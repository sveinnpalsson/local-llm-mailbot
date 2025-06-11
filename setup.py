from setuptools import setup, find_packages

setup(
    name="mailbot",
    version="0.1.0",
    package_dir={"": "src"},
    packages=find_packages(where="src"),
    install_requires=[
        "requests>=2.28.2",
        "google-api-python-client>=2.70.0",
        "google-auth>=2.17.3",
        "google-auth-oauthlib>=0.7.1",
        "google-auth-httplib2>=0.1.0",
        "telethon>=1.28.0",
        "sqlcipher3-binary>=0.5.4",
    ],
    entry_points={
        "console_scripts": [
            "mailbot-profile-builder=mailbot.profile_builder:main",
            "mailbot-listen=mailbot.main:main",
        ],
    },
)
[]