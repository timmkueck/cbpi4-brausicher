from setuptools import setup

setup(
    name="cbpi4-brausicher",
    version="0.1.0",
    description="CraftBeerPi4: sichere Fühler und Aktoren (lgpio, Heizschutz, Wächter), Fortsetzen nach Neustart",
    author="Timm Kück",
    url="https://github.com/timmkueck/cbpi4-brausicher",
    packages=["cbpi4_brausicher"],
    install_requires=["lgpio>=0.2.2"],
    python_requires=">=3.9",
)
