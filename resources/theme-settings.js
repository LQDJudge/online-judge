$(function() {
  'use strict';

  var csrfToken = $('#csrf-token').val();
  var uploadUrl = $('#upload-url').val();
  var deleteUrl = $('#delete-url').val();
  var toggleDarkmodeUrl = $('#toggle-darkmode-url').val();

  // Dark mode toggle
  $('.mode-option').on('click', function() {
    var mode = $(this).data('mode');

    // Skip if already on this mode
    if ($(this).hasClass('active')) {
      return;
    }

    // Toggle visually immediately for better UX
    $('.mode-option').removeClass('active');
    $(this).addClass('active');

    // Send AJAX request to set dark mode
    $.ajax({
      url: toggleDarkmodeUrl,
      type: 'POST',
      data: {
        csrfmiddlewaretoken: csrfToken,
        mode: mode
      },
      success: function() {
        // Reload to apply dark mode CSS
        location.reload();
      },
      error: function() {
        alert('Failed to toggle dark mode. Please try again.');
        location.reload();
      }
    });
  });

  // Superuser: Upload sample background
  $('#upload-sample-btn').on('click', function() {
    var fileInput = $('#sample-upload-file')[0];

    if (!fileInput.files.length) {
      alert('Please select a file to upload.');
      return;
    }

    var file = fileInput.files[0];

    // Validate file type
    var allowedTypes = ['image/jpeg', 'image/png', 'image/webp', 'image/gif'];
    if (allowedTypes.indexOf(file.type) === -1) {
      alert('Invalid file type. Please upload a JPG, PNG, WebP, or GIF image.');
      return;
    }

    // Validate file size (10MB)
    if (file.size > 10 * 1024 * 1024) {
      alert('File too large. Maximum size is 10MB.');
      return;
    }

    var formData = new FormData();
    formData.append('image', file);
    formData.append('csrfmiddlewaretoken', csrfToken);

    var $btn = $(this);
    $btn.prop('disabled', true).html('<i class="fa fa-spinner fa-spin"></i> Uploading...');

    $.ajax({
      url: uploadUrl,
      type: 'POST',
      data: formData,
      processData: false,
      contentType: false,
      success: function(response) {
        if (response.success) {
          location.reload();
        } else {
          alert('Upload failed: ' + (response.error || 'Unknown error'));
          $btn.prop('disabled', false).html('<i class="fa fa-upload"></i> Upload Sample');
        }
      },
      error: function(xhr) {
        var error = 'Upload failed.';
        if (xhr.responseJSON && xhr.responseJSON.error) {
          error = xhr.responseJSON.error;
        }
        alert(error);
        $btn.prop('disabled', false).html('<i class="fa fa-upload"></i> Upload Sample');
      }
    });
  });

  // Superuser: Delete sample background
  $('.delete-bg-btn').on('click', function() {
    var filename = $(this).data('filename');

    if (!confirm('Are you sure you want to delete this background?')) {
      return;
    }

    var $item = $(this).closest('.admin-bg-item');

    $.ajax({
      url: deleteUrl,
      type: 'POST',
      data: {
        filename: filename,
        csrfmiddlewaretoken: csrfToken
      },
      success: function(response) {
        if (response.success) {
          $item.fadeOut(300, function() {
            $(this).remove();
          });
        } else {
          alert('Delete failed: ' + (response.error || 'Unknown error'));
        }
      },
      error: function(xhr) {
        var error = 'Delete failed.';
        if (xhr.responseJSON && xhr.responseJSON.error) {
          error = xhr.responseJSON.error;
        }
        alert(error);
      }
    });
  });
});
