"""
Simple verification script to test SeisBench installation and pretrained models.
"""

import sys

def main():
    print("Checking dependencies...")
    try:
        import torch
        print(f"✓ PyTorch version: {torch.__version__} (CUDA available: {torch.cuda.is_available()})")
    except ImportError:
        print("✗ PyTorch is not installed. Install via: pip install torch")
        sys.exit(1)

    try:
        import obspy
        print(f"✓ ObsPy version: {obspy.__version__}")
    except ImportError:
        print("✗ ObsPy is not installed. Install via: pip install obspy")
        sys.exit(1)

    try:
        import seisbench
        import seisbench.models as sbm
        print(f"✓ SeisBench version: {seisbench.__version__}")
    except ImportError:
        print("✗ SeisBench is not installed. Install via: pip install seisbench")
        sys.exit(1)

    print("\nLoading pretrained PhaseNet model ('stead')...")
    try:
        model = sbm.PhaseNet.from_pretrained("stead")
        print(f"✓ Successfully loaded model: {model.__class__.__name__}")
        print("SeisBench environment is ready to use!")
    except Exception as e:
        print(f"✗ Failed to download/load pretrained model: {e}")

if __name__ == "__main__":
    main()
