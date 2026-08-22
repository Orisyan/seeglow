"""PyInstaller 打包入口：python run_web.py 等价于 python -m seeglow --web"""
from seeglow.cli import main

if __name__ == "__main__":
    main()
