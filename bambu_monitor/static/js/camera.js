/* camera.js - 摄像头定时刷新 */

const CAMERA_REFRESH_MS = 3000;

function initCameras(serials) {
    refreshCameras(serials);
    setInterval(() => refreshCameras(serials), CAMERA_REFRESH_MS);
}

function refreshCameras(serials) {
    serials.forEach(serial => {
        const img = document.querySelector(`.camera-img[data-serial="${serial}"]`);
        if (!img) return;

        const newImg = new Image();
        newImg.onload = () => {
            img.src = newImg.src;
        };
        newImg.onerror = () => {
            // 保持当前图像或显示离线占位
        };
        newImg.src = `/api/printers/${serial}/camera?t=${Date.now()}`;
    });
}
