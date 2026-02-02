/**
 * Direct Upload Utility
 *
 * Universal file upload handler that works with S3/R2 presigned URLs
 * and local storage fallback. Used by DirectUploadWidget and Pagedown.
 */

/**
 * DirectUploader class - handles file uploads to any storage backend.
 */
class DirectUploader {
    constructor(options) {
        this.configUrl = options.configUrl;
        this.saveUrl = options.saveUrl;
        this.onProgress = options.onProgress || (() => {});
        this.onSuccess = options.onSuccess || (() => {});
        this.onError = options.onError || (() => {});
    }

    /**
     * Upload a file to storage.
     */
    async upload(file, uploadInfo) {
        try {
            const config = await this.getConfig(file, uploadInfo);
            await this.uploadFile(file, config);

            if (uploadInfo && uploadInfo.uploadToken) {
                await this.saveToModel(config.file_key, uploadInfo.uploadToken);
            }

            this.onSuccess(config);
            return config;
        } catch (error) {
            this.onError(error);
            throw error;
        }
    }

    async getConfig(file, uploadInfo) {
        const csrfToken = getCsrfToken();
        const body = {
            upload_token: uploadInfo.uploadToken,
            filename: file.name,
            content_type: file.type || 'application/octet-stream',
            file_size: file.size,
        };

        const response = await fetch(this.configUrl, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrfToken,
            },
            body: JSON.stringify(body),
        });

        if (!response.ok) {
            const data = await response.json().catch(() => ({}));
            throw new Error(data.error || gettext('Failed to get upload configuration'));
        }

        return response.json();
    }

    uploadFile(file, config) {
        return new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();

            xhr.upload.onprogress = (e) => {
                if (e.lengthComputable) {
                    this.onProgress(e.loaded / e.total);
                }
            };

            xhr.onload = () => {
                if (xhr.status >= 200 && xhr.status < 300) {
                    resolve();
                } else {
                    reject(new Error(gettext('Upload failed: ') + xhr.statusText));
                }
            };

            xhr.onerror = () => reject(new Error(gettext('Network error during upload')));
            xhr.ontimeout = () => reject(new Error(gettext('Upload timed out')));

            if (config.storage_type === 's3') {
                // S3/R2: Use PUT with presigned URL
                xhr.open('PUT', config.upload_url);
                xhr.setRequestHeader('Content-Type', config.content_type || file.type);
                xhr.send(file);
            } else {
                const formData = new FormData();
                formData.append('file', file);
                xhr.open('POST', config.upload_url);
                xhr.setRequestHeader('X-Upload-Token', config.token);
                xhr.setRequestHeader('X-CSRFToken', getCsrfToken());
                xhr.send(formData);
            }
        });
    }

    async saveToModel(fileKey, uploadToken) {
        const response = await fetch(this.saveUrl, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': getCsrfToken(),
            },
            body: JSON.stringify({
                file_key: fileKey,
                upload_token: uploadToken,
            }),
        });

        if (!response.ok) {
            const data = await response.json().catch(() => ({}));
            throw new Error(data.error || gettext('Failed to save file to model'));
        }

        return response.json();
    }
}


/**
 * Widget Controller - manages widget state and DOM updates.
 */
class DirectUploadWidgetController {
    constructor(widget) {
        this.widget = widget;
        this.fileUrl = null;
        this.fileKey = null;

        // Cache config from data attributes
        this.config = {
            configUrl: widget.dataset.configUrl,
            saveUrl: widget.dataset.saveUrl,
            deleteUrl: widget.dataset.deleteUrl,
            uploadTo: widget.dataset.uploadTo,
            prefix: widget.dataset.prefix,
            maxSize: parseInt(widget.dataset.maxSize) || 0,
            accept: widget.dataset.accept || '*/*',
            uploadToken: widget.dataset.uploadToken,
            // Widget type from Python
            widgetType: widget.dataset.widgetType || 'file',
            showFullPath: widget.dataset.showFullPath === 'True',
            // UI text from Python
            chooseText: widget.dataset.chooseText || gettext('Choose a file'),
            changeText: widget.dataset.changeText || gettext('Change'),
            removeText: widget.dataset.removeText || gettext('Remove'),
        };

        // Detect initial state
        this.state = this._detectInitialState();

        // Create uploader
        this.uploader = new DirectUploader({
            configUrl: this.config.configUrl,
            saveUrl: this.config.saveUrl,
            onProgress: (pct) => this._onProgress(pct),
            onSuccess: (config) => this._onSuccess(config),
            onError: (err) => this._onError(err),
        });

        this._bindEvents();
    }

    _createActionsHtml() {
        return `
            <label class="change-btn">
                <i class="fa fa-upload"></i>
                <span>${this.config.changeText}</span>
                <input type="file" class="hidden-file-input" accept="${this.config.accept}">
            </label>
            <button type="button" class="delete-btn">
                <i class="fa fa-trash"></i>
                <span>${this.config.removeText}</span>
            </button>
        `;
    }

    _createEmptyAreaHtml() {
        return `
            <i class="fa fa-cloud-upload"></i>
            <span>${this.config.chooseText}</span>
            <input type="file" class="hidden-file-input" accept="${this.config.accept}">
        `;
    }

    _createPreviewHtml(url, displayName) {
        if (this.config.widgetType === 'image') {
            return `
                <a href="${url}" data-featherlight="image" data-featherlight-variant="image-widget-lightbox">
                    <img src="${url}" style="max-width: 150px; max-height: 150px;" alt="${gettext('Uploaded image')}">
                    <div class="image-overlay"><i class="fa fa-search-plus"></i></div>
                </a>
            `;
        }
        // Default: file/PDF preview
        return `
            <i class="fa fa-file-pdf fa-lg pdf-icon"></i>
            <a href="${url}" target="_blank">${displayName}</a>
        `;
    }

    _getDisplayName(fileKey) {
        if (!fileKey) return 'file';
        return this.config.showFullPath ? fileKey : fileKey.split('/').pop();
    }

    _detectInitialState() {
        const preview = this.widget.querySelector('.current-preview');

        if (preview) {
            // Capture existing URL
            const link = preview.querySelector('a');
            if (link) this.fileUrl = link.href;
            // Try to get file key from hidden input
            const hiddenInput = this.widget.querySelector('.file-key-input');
            if (hiddenInput && hiddenInput.value) {
                this.fileKey = hiddenInput.value;
            }
            return 'hasFile';
        }
        return 'empty';
    }

    _bindEvents() {
        // Bind to all file inputs in the widget
        this._bindFileInputs();
        this._bindDeleteButton();
        this._bindRetryButton();
    }

    _bindFileInputs() {
        const inputs = this.widget.querySelectorAll('input[type="file"]');
        inputs.forEach(input => {
            if (input.hasAttribute('data-bound')) return;
            input.setAttribute('data-bound', 'true');
            input.addEventListener('change', (e) => this._onFileSelect(e));
        });
    }

    _bindDeleteButton() {
        const btn = this.widget.querySelector('.delete-btn');
        if (btn && !btn.hasAttribute('data-bound')) {
            btn.setAttribute('data-bound', 'true');
            btn.addEventListener('click', () => this._onDelete());
        }
    }

    _bindRetryButton() {
        const btn = this.widget.querySelector('.retry-btn');
        if (btn && !btn.hasAttribute('data-bound')) {
            btn.setAttribute('data-bound', 'true');
            btn.addEventListener('click', () => {
                this._hideError();
                this._getFileInput()?.click();
            });
        }
    }

    _getFileInput() {
        return this.widget.querySelector('input[type="file"]');
    }

    _onFileSelect(e) {
        const file = e.target.files[0];
        if (!file) return;

        // Validate
        if (this.config.maxSize && file.size > this.config.maxSize) {
            this._showStatus('error', gettext('File too large (max ') + formatFileSize(this.config.maxSize) + ')');
            return;
        }

        if (!validateFileType(file, this.config.accept)) {
            this._showStatus('error', gettext('Invalid file type'));
            return;
        }

        // Start upload
        this._setState('uploading');

        const uploadInfo = {
            uploadToken: this.config.uploadToken,
        };

        this.uploader.upload(file, uploadInfo).catch(() => {
            // Error handled by onError callback
        });
    }

    _onProgress(pct) {
        this._showProgress(pct);
    }

    _onSuccess(config) {
        this.fileUrl = config.file_url;
        this.fileKey = config.file_key;
        this._setState('hasFile');
        this._showStatus('success', gettext('Uploaded successfully'));
    }

    _onError(err) {
        this._setState('error');
        this._showStatus('error', err.message);
    }

    async _onDelete() {
        if (!this.config.uploadToken || !this.config.deleteUrl) {
            this._showStatus('error', gettext('Cannot delete: missing configuration'));
            return;
        }

        if (!confirm(gettext('Are you sure you want to delete this file?'))) {
            return;
        }

        try {
            const response = await fetch(this.config.deleteUrl, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-CSRFToken': getCsrfToken(),
                },
                body: JSON.stringify({ upload_token: this.config.uploadToken }),
            });

            if (!response.ok) {
                const data = await response.json().catch(() => ({}));
                throw new Error(data.error || gettext('Failed to delete file'));
            }

            this.fileUrl = null;
            this.fileKey = null;
            this._setState('empty');
            this._showStatus('success', gettext('File deleted'));
        } catch (err) {
            this._showStatus('error', err.message);
        }
    }

    _setState(newState) {
        this.state = newState;
        this._render();
    }

    _render() {
        switch (this.state) {
            case 'empty':
                this._renderEmpty();
                break;
            case 'uploading':
                this._renderUploading();
                break;
            case 'hasFile':
                this._renderHasFile();
                break;
            case 'error':
                this._renderError();
                break;
        }
    }

    _renderEmpty() {
        this._hideProgress();
        this._hideError();

        // Remove current preview and actions
        this.widget.querySelector('.current-preview')?.remove();
        this.widget.querySelector('.preview-actions')?.remove();

        // Show or create upload area
        let uploadArea = this.widget.querySelector('.upload-area');
        if (!uploadArea) {
            uploadArea = this._createUploadArea();
            const progressEl = this.widget.querySelector('.upload-progress');
            this.widget.insertBefore(uploadArea, progressEl);
        }
        uploadArea.style.display = '';

        // Update hidden input
        const hiddenInput = this.widget.querySelector('.file-key-input');
        if (hiddenInput) hiddenInput.value = '';

        this._bindFileInputs();
    }

    _renderUploading() {
        const uploadArea = this.widget.querySelector('.upload-area');
        if (uploadArea) uploadArea.style.display = 'none';
        this._hideError();
    }

    _renderHasFile() {
        this._hideProgress();
        this._hideError();

        // Remove empty state upload area
        this.widget.querySelector('.upload-area')?.remove();

        this._renderPreview();

        // Update hidden input
        const hiddenInput = this.widget.querySelector('.file-key-input');
        if (hiddenInput) hiddenInput.value = this.fileKey || '';

        this._bindFileInputs();
        this._bindDeleteButton();
    }

    _renderError() {
        const uploadArea = this.widget.querySelector('.upload-area');
        if (uploadArea) uploadArea.style.display = '';
        this._hideProgress();
    }

    _renderPreview() {
        let preview = this.widget.querySelector('.current-preview');
        let actions = this.widget.querySelector('.preview-actions');
        const displayName = this._getDisplayName(this.fileKey);

        if (preview) {
            // Update existing preview
            const link = preview.querySelector('a');
            const img = preview.querySelector('img');
            if (link) link.href = this.fileUrl;
            if (img) {
                img.src = this.fileUrl;
                img.removeAttribute('width');
                img.removeAttribute('height');
                img.style.maxWidth = '150px';
                img.style.maxHeight = '150px';
            }
            // Update link text for non-image
            if (link && !img) link.textContent = displayName;
        } else {
            // Create new preview
            preview = document.createElement('div');
            preview.className = 'current-preview';
            preview.innerHTML = this._createPreviewHtml(this.fileUrl, displayName);

            // Create actions
            actions = document.createElement('div');
            actions.className = 'preview-actions';
            actions.innerHTML = this._createActionsHtml();

            const progressEl = this.widget.querySelector('.upload-progress');
            this.widget.insertBefore(preview, progressEl);
            this.widget.insertBefore(actions, progressEl);
        }
    }

    _createUploadArea() {
        const label = document.createElement('label');
        label.className = 'upload-area';
        label.innerHTML = this._createEmptyAreaHtml();
        return label;
    }

    _showProgress(pct) {
        const progressEl = this.widget.querySelector('.upload-progress');
        const progressFill = this.widget.querySelector('.progress-bar-fill');
        const progressPercent = this.widget.querySelector('.progress-percent');

        if (progressEl) progressEl.style.display = 'block';
        if (progressFill) progressFill.style.width = (pct * 100) + '%';
        if (progressPercent) progressPercent.textContent = Math.round(pct * 100) + '%';
    }

    _hideProgress() {
        const progressEl = this.widget.querySelector('.upload-progress');
        if (progressEl) progressEl.style.display = 'none';
    }

    _hideError() {
        const errorEl = this.widget.querySelector('.upload-error');
        if (errorEl) errorEl.style.display = 'none';
    }

    _showStatus(type, message) {
        let statusEl = this.widget.querySelector('.upload-status');
        if (!statusEl) {
            statusEl = document.createElement('div');
            statusEl.className = 'upload-status';
            this.widget.appendChild(statusEl);
        }

        const icon = type === 'success' ? 'fa-check-circle' : 'fa-exclamation-circle';
        statusEl.className = `upload-status upload-status-${type}`;
        statusEl.innerHTML = `<i class="fa ${icon}"></i><span>${message}</span>`;
        statusEl.style.display = 'flex';

        if (type === 'success') {
            setTimeout(() => { statusEl.style.display = 'none'; }, 3000);
        }
    }
}


/**
 * Helper functions
 */
function getCsrfToken() {
    const cookies = document.cookie.split(';');
    for (let cookie of cookies) {
        const [name, value] = cookie.trim().split('=');
        if (name === 'csrftoken') return value;
    }
    return '';
}

function formatFileSize(bytes) {
    if (bytes === 0) return '0 ' + gettext('Bytes');
    const k = 1024;
    const sizes = [gettext('Bytes'), 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

function validateFileType(file, accept) {
    if (!accept || accept === '*/*') return true;

    const acceptTypes = accept.split(',').map(t => t.trim().toLowerCase());
    const fileType = file.type.toLowerCase();
    const fileExt = '.' + file.name.split('.').pop().toLowerCase();

    for (const acceptType of acceptTypes) {
        if (acceptType === fileType) return true;
        if (acceptType.endsWith('/*')) {
            const category = acceptType.slice(0, -2);
            if (fileType.startsWith(category + '/')) return true;
        }
        if (acceptType.startsWith('.') && acceptType === fileExt) return true;
    }

    return false;
}


/**
 * Initialize widgets
 */
function initDirectUploadWidget(widget) {
    if (widget.hasAttribute('data-controller-initialized')) return;
    widget.setAttribute('data-controller-initialized', 'true');
    new DirectUploadWidgetController(widget);
}

// Auto-initialize on DOMContentLoaded
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-direct-upload]').forEach(initDirectUploadWidget);
});

// Initialize dynamically added widgets
if (typeof MutationObserver !== 'undefined') {
    const observer = new MutationObserver((mutations) => {
        mutations.forEach((mutation) => {
            mutation.addedNodes.forEach((node) => {
                if (node.nodeType === Node.ELEMENT_NODE) {
                    if (node.hasAttribute && node.hasAttribute('data-direct-upload')) {
                        initDirectUploadWidget(node);
                    }
                    node.querySelectorAll && node.querySelectorAll('[data-direct-upload]').forEach(initDirectUploadWidget);
                }
            });
        });
    });
    observer.observe(document.body, { childList: true, subtree: true });
}

// Export for use in other scripts
window.DirectUploader = DirectUploader;
window.DirectUploadWidgetController = DirectUploadWidgetController;
window.initDirectUploadWidget = initDirectUploadWidget;
