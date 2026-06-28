"""安装配置"""
from setuptools import setup, find_packages

setup(
    name="trading-agent",
    version="1.0.0",
    description="AI-Agent A股智能分析系统",
    long_description=open("README.md", encoding="utf-8").read(),
    long_description_content_type="text/markdown",
    author="George-kiki",
    license="MIT",
    python_requires=">=3.9",
    packages=find_packages(),
    py_modules=["main", "scheduler"],
    install_requires=[
        "akshare>=1.12.0",
        "pandas>=2.0.0",
        "numpy>=1.24.0",
        "APScheduler>=3.10.0",
        "openai>=1.30.0",
        "python-dotenv>=1.0.0",
        "fastapi>=0.110.0",
        "uvicorn[standard]>=0.29.0",
        "jinja2>=3.1.0",
        "python-multipart>=0.0.9",
        "pydantic>=2.0.0",
        "pydantic-settings>=2.0.0",
        "rich>=13.0.0",
        "tabulate>=0.9.0",
        "requests>=2.31.0",
    ],
    entry_points={
        "console_scripts": [
            "trading-agent=main:main",
        ],
    },
)
