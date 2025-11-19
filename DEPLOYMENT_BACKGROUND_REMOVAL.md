# Background Removal Deployment Guide

## Problem Solved
Replaced `rembg` library which caused TensorFlow/ONNX conflicts and warnings on Ubuntu server.

## Solution
Migrated to `transparent-background` - a PyTorch-only library with:
- **No TensorFlow dependencies** (eliminates ONNX/TensorFlow conflicts)
- **Faster inference** with JIT compilation
- **Better quality** results
- **Smaller memory footprint**

## Changes Made

### 1. Backend Code (`backend/routers/background_removal.py`)
- Replaced `rembg` with `transparent-background.Remover`
- Simplified initialization (no session management needed)
- Uses PyTorch GPU acceleration when available

### 2. Dependencies (`backend/requirements.txt`)
**Removed:**
```
rembg==2.0.57
onnxruntime-gpu==1.18.1
timm==0.9.16
```

**Added:**
```
transparent-background==1.3.4
```

**Kept:**
```
segment-anything==1.0  (for Mobile-SAM refinement step)
```

## Deployment Steps

### On Ubuntu Server:

1. **Stop the service:**
```bash
sudo systemctl stop photomark
```

2. **Pull latest code:**
```bash
cd ~/photomark-backend
git pull origin main
```

3. **Update dependencies:**
```bash
pip install -r requirements.txt
```

4. **Uninstall old packages (optional but recommended):**
```bash
pip uninstall rembg onnxruntime-gpu timm -y
```

5. **Download model (first run only):**
The model will auto-download on first use to `~/.transparent-background/models/`

6. **Restart service:**
```bash
sudo systemctl start photomark
sudo systemctl status photomark
```

7. **Check logs (should see clean startup):**
```bash
sudo journalctl -u photomark -f
```

## Expected Improvements

✅ **No more TensorFlow warnings:**
- No `device_discovery.cc` errors
- No XNNPACK delegate messages
- No inference feedback manager warnings

✅ **Better performance:**
- Faster model loading
- Lower memory usage
- GPU acceleration with PyTorch CUDA

✅ **Same API interface:**
- All endpoints remain unchanged
- Frontend requires no modifications

## Testing

Test the endpoint with:
```bash
curl -X POST "http://localhost:8000/api/background-removal/step1-rembg" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -F "image=@test_image.jpg"
```

Or test from the frontend at: `http://localhost:5173/background-removal`

## Rollback (if needed)

If issues arise, revert to previous version:
```bash
pip install rembg==2.0.57 onnxruntime-gpu==1.18.1 timm==0.9.16
git checkout HEAD~1 backend/routers/background_removal.py
sudo systemctl restart photomark
```
