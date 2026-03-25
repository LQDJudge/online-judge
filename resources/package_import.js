$(document).ready(function () {
  var config = window.IMPORT_CONFIG;
  var t = config.i18n || {};
  var $fileInput = $('#import-file-input');
  var $uploadBtn = $('#import-upload-btn');
  var $status = $('#import-status');
  var $uploadSection = $('#import-upload-section');
  var $results = $('#import-results');
  var $fields = $('#import-fields');
  var $summary = $('#import-summary');

  var importData = null;

  $fileInput.change(function () {
    $uploadBtn.prop('disabled', !this.files.length);
  });

  // Restore previous results if a task ID exists
  if (config.lastTaskId) {
    showStatus('analyzing', '<i class="fa fa-spinner fa-spin"></i> ' + (t.loading || 'Loading...'));
    pollTask(config.lastTaskId);
  }

  $uploadBtn.click(function () {
    var file = $fileInput[0].files[0];
    if (!file) return;

    if (file.size > config.maxUploadSize) {
      showStatus('error', (t.fileTooLarge || 'File too large') + ' (' + (file.size / 1024 / 1024).toFixed(1) + ' MB)');
      return;
    }

    $uploadBtn.prop('disabled', true);
    showStatus('analyzing', '<i class="fa fa-spinner fa-spin"></i> ' + (t.uploading || 'Uploading...'));

    var formData = new FormData();
    formData.append('package_file', file);
    formData.append('csrfmiddlewaretoken', config.csrfToken);

    $.ajax({
      url: config.uploadUrl,
      type: 'POST',
      data: formData,
      processData: false,
      contentType: false,
      success: function (data) {
        if (data.success && data.task_id) {
          showStatus('analyzing', '<i class="fa fa-spinner fa-spin"></i> ' + (t.processing || 'Processing...'));
          pollTask(data.task_id);
        } else {
          showStatus('error', data.error || (t.uploadFailed || 'Upload failed'));
          $uploadBtn.prop('disabled', false);
        }
      },
      error: function (xhr) {
        var msg = t.uploadFailed || 'Upload failed';
        try { msg = JSON.parse(xhr.responseText).error || msg; } catch (e) {}
        showStatus('error', msg);
        $uploadBtn.prop('disabled', false);
      }
    });
  });

  function showStatus(type, html) {
    $status.attr('class', 'import-status show ' + type).html(html);
  }

  function pollTask(taskId) {
    $.get(config.taskStatusUrl + '?id=' + taskId, function (data) {
      if (data.code === 'SUCCESS') {
        if (data.success) {
          importData = data;
          showStatus('done', '<i class="fa fa-check"></i> ' + (t.complete || 'Complete!'));
          renderResults(data);
        } else {
          var errMsg = data.error || (t.failed || 'Failed');
          var isNoFiles = errMsg.indexOf('did not return any files') >= 0;
          var hint = isNoFiles ? ' ' + (t.retryHint || 'Please try again.') : '';
          showStatus('error', '<i class="fa fa-exclamation-triangle"></i> ' + errMsg + hint);
          $uploadBtn.prop('disabled', false);
        }
      } else if (data.code === 'FAILURE') {
        showStatus('error', '<i class="fa fa-exclamation-triangle"></i> ' + (t.taskFailed || 'Task failed'));
        $uploadBtn.prop('disabled', false);
      } else {
        setTimeout(function () { pollTask(taskId); }, 4000);
      }
    }).fail(function () {
      showStatus('error', t.lostConnection || 'Lost connection');
      $uploadBtn.prop('disabled', false);
    });
  }

  function renderResults(result) {
    var summary = result.summary || {};
    var files = result.files || {};
    var saveDir = result.save_dir;

    // Build summary box
    var summaryHtml = '<div class="import-summary-box">';
    if (summary.format) {
      summaryHtml += '<div class="import-summary-row">';
      summaryHtml += '<span class="import-summary-label">' + (t.format || 'Format') + ':</span> ';
      summaryHtml += '<span class="import-summary-value">' + escapeHtml(summary.format) + '</span>';
      summaryHtml += '</div>';
    }
    if (summary.problem_name) {
      summaryHtml += '<div class="import-summary-row">';
      summaryHtml += '<span class="import-summary-label">' + (t.problem || 'Problem') + ':</span> ';
      summaryHtml += '<span class="import-summary-value">' + escapeHtml(summary.problem_name) + '</span>';
      summaryHtml += '</div>';
    }
    if (summary.test_count) {
      var testInfo = summary.test_count + ' ' + (t.tests || 'tests');
      if (summary.sample_count) testInfo += ' (' + summary.sample_count + ' ' + (t.samples || 'samples') + ')';
      summaryHtml += '<div class="import-summary-row">';
      summaryHtml += '<span class="import-summary-label">' + (t.tests || 'Tests') + ':</span> ';
      summaryHtml += '<span class="import-summary-value">' + testInfo + '</span>';
      summaryHtml += '</div>';
    }
    if (summary.solutions && summary.solutions.length > 0) {
      summaryHtml += '<div class="import-summary-row">';
      summaryHtml += '<span class="import-summary-label">' + (t.solutions || 'Solutions') + ':</span> ';
      summaryHtml += '<span class="import-summary-value">' + summary.solutions.length + ' ' + (t.found || 'found') + '</span>';
      summaryHtml += '</div>';
    }
    // No notes — removed as requested
    summaryHtml += '</div>';
    $summary.html(summaryHtml);

    // Render fields
    $fields.empty();

    if (files.description) {
      var descTitle = t.description || 'Description';
      var images = files.images || [];
      if (images.length > 0) {
        descTitle += ' + ' + images.length + ' ' + (images.length > 1 ? (t.images || 'images') : (t.image || 'image'));
      }
      addField('description', 'fa-file-alt', descTitle, files.description, saveDir, true, function (body, file) {
        loadFilePreview(body, file.path, false);
        if (images.length > 0) {
          var imgHtml = '<div style="margin-top: 10px; border-top: 1px solid #eee; padding-top: 10px;">';
          imgHtml += '<strong>' + (t.imagesIncluded || 'Images included') + ':</strong><br>';
          images.forEach(function (img) {
            imgHtml += '<span style="display: inline-block; margin: 4px 8px 4px 0; font-size: 0.85em;">';
            imgHtml += '<i class="fa fa-image"></i> ' + escapeHtml(img.name);
            imgHtml += ' (' + (img.size / 1024).toFixed(1) + ' KB)';
            imgHtml += '</span>';
          });
          imgHtml += '</div>';
          body.append(imgHtml);
        }
      });
    }

    if (summary.time_limit_seconds) {
      addValueField('time_limit', 'fa-clock', t.timeLimit || 'Time Limit', summary.time_limit_seconds + 's', summary.time_limit_seconds, saveDir);
    }

    if (summary.memory_limit_mb) {
      var maxMb = 1024;
      var memMb = Math.min(summary.memory_limit_mb, maxMb);
      var memLabel = memMb + ' MB';
      if (summary.memory_limit_mb > maxMb) {
        memLabel += ' (' + (t.cappedFrom || 'capped from') + ' ' + summary.memory_limit_mb + ' MB)';
      }
      addValueField('memory_limit', 'fa-memory', t.memoryLimit || 'Memory Limit', memLabel, memMb, saveDir);
    }

    if (files.testdata) {
      var testLabel = t.testData || 'Test Data';
      if (summary.test_count) testLabel += ' (' + summary.test_count + ' ' + (t.tests || 'tests') + ')';
      addField('testdata', 'fa-database', testLabel, files.testdata, saveDir, false, function (body, file) {
        body.html('<div class="import-field-preview">ZIP: ' + escapeHtml(file.name) + ' (' + (file.size / 1024).toFixed(1) + ' KB)</div>');
      });
    }

    if (files.checker) {
      addField('checker', 'fa-check-circle', t.checker || 'Checker', files.checker, saveDir, true);
    }

    if (files.generator) {
      addField('generator', 'fa-cogs', t.generator || 'Generator', files.generator, saveDir, true);
    }

    if (files.generator_script) {
      addField('generator_script', 'fa-list-ol', t.generatorScript || 'Generator Script', files.generator_script, saveDir, false);
    }

    if (files.interactive) {
      addField('interactive', 'fa-exchange-alt', t.interactive || 'Interactive Judge', files.interactive, saveDir, true);
    }

    var solutions = files.solutions || [];
    for (var i = 0; i < solutions.length; i++) {
      (function (idx, sol) {
        addField('solution_' + idx, 'fa-code', sol.name, sol, saveDir, true, null, {filename: sol.name});
      })(i, solutions[i]);
    }

    $results.addClass('show');
  }

  function addField(fieldName, icon, title, fileInfo, saveDir, isCode, customPreview, extraData) {
    var $field = $('<div class="import-field"></div>');
    var $header = $('<div class="import-field-header"></div>');
    var $body = $('<div class="import-field-body"></div>');
    var $applyBtn = $('<button class="action-btn" style="font-size: 0.85em; padding: 4px 12px;">Apply</button>');
    var $appliedLabel = $('<span class="import-applied" style="display:none;"><i class="fa fa-check"></i> Applied</span>');

    var $titleArea = $('<div class="import-field-title-area"></div>');
    $titleArea.html(
      '<span class="import-field-title"><i class="fa ' + icon + '"></i> ' + escapeHtml(title) + '</span>' +
      '<span class="import-field-meta">' + (fileInfo.size ? (fileInfo.size / 1024).toFixed(1) + ' KB' : '') + '</span>'
    );

    $header.append($titleArea).append($appliedLabel).append($applyBtn);

    $titleArea.css('cursor', 'pointer').css('flex', '1').click(function () {
      $body.toggleClass('show');
      if ($body.hasClass('show') && !$body.data('loaded')) {
        if (customPreview) {
          customPreview($body, fileInfo);
        } else {
          loadFilePreview($body, fileInfo.path, isCode);
        }
        $body.data('loaded', true);
      }
    });

    $applyBtn.click(function (e) {
      e.stopPropagation();
      var btn = $(this);
      btn.prop('disabled', true).html('<i class="fa fa-spinner fa-spin"></i>');

      var postData = {
        field: fieldName,
        save_dir: saveDir,
        csrfmiddlewaretoken: config.csrfToken,
      };
      if (extraData) $.extend(postData, extraData);

      $.post(config.applyUrl, postData, function (data) {
        if (data.success) {
          btn.hide();
          $appliedLabel.show();
        } else {
          btn.prop('disabled', false).html('Apply');
          alert('Error: ' + (data.error || 'Unknown error'));
        }
      }).fail(function () {
        btn.prop('disabled', false).html('Apply');
        alert('Failed to apply');
      });
    });

    $field.append($header).append($body);
    $fields.append($field);
  }

  function addValueField(fieldName, icon, title, displayValue, rawValue, saveDir) {
    var $field = $('<div class="import-field"></div>');
    var $header = $('<div class="import-field-header"></div>');
    var $applyBtn = $('<button class="action-btn" style="font-size: 0.85em; padding: 4px 12px;">Apply</button>');
    var $appliedLabel = $('<span class="import-applied" style="display:none;"><i class="fa fa-check"></i> Applied</span>');

    $header.html(
      '<span class="import-field-title"><i class="fa ' + icon + '"></i> ' + escapeHtml(title) + ': ' + escapeHtml(String(displayValue)) + '</span>'
    );
    $header.append($appliedLabel).append($applyBtn);

    $applyBtn.click(function (e) {
      e.stopPropagation();
      var btn = $(this);
      btn.prop('disabled', true).html('<i class="fa fa-spinner fa-spin"></i>');

      $.post(config.applyUrl, {
        field: fieldName,
        save_dir: saveDir,
        value: rawValue,
        csrfmiddlewaretoken: config.csrfToken,
      }, function (data) {
        if (data.success) {
          btn.hide();
          $appliedLabel.show();
        } else {
          btn.prop('disabled', false).html('Apply');
          alert('Error: ' + (data.error || 'Unknown error'));
        }
      }).fail(function () {
        btn.prop('disabled', false).html('Apply');
        alert('Failed to apply');
      });
    });

    $field.append($header);
    $fields.append($field);
  }

  function loadFilePreview(container, filePath, isCode) {
    $.get(config.filePreviewUrl, {path: filePath}, function (content) {
      if (isCode) {
        container.html('<pre class="import-field-preview"><code>' + escapeHtml(content) + '</code></pre>');
      } else {
        container.html('<div class="import-field-preview">' + escapeHtml(content) + '</div>');
      }
    }).fail(function () {
      container.html('<div class="import-field-preview">(Preview not available)</div>');
    });
  }

  function escapeHtml(text) {
    var div = document.createElement('div');
    div.appendChild(document.createTextNode(text));
    return div.innerHTML;
  }
});
