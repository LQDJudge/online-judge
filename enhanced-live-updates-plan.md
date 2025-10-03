# Enhanced Live Submission Updates - Hybrid Implementation Plan

## Overview

This document outlines a hybrid plan that combines multiple secure channels with simplified settings to extend live submission updates beyond public submissions to include all submission types while maintaining security and handling pagination properly.

## Current System Limitations

### Backend Limitations

- **Public-only updates**: Line 983 in `_post_update_submission()` only sends to `submissions` channel if `problem__is_public = True`
- **Organization-private vulnerability**: Current code ignores `is_organization_private` field, causing 404 errors and information leakage
- **No contest submission updates**: Contest submissions don't get live updates in submission lists
- **Missing updates for authorized users**: Users who can see private problems/contests don't get live updates

### Frontend Limitations

- **Page 1 only**: Dynamic updates only enabled when `context["page_obj"].number == 1`
- **New submissions prepended**: Lines 174-178 always prepend new submissions, breaking pagination order
- **No pagination awareness**: Updates don't consider which page a submission belongs to

## Hybrid Solution Architecture

### 1. Multi-Channel Backend with Shared Secret

#### Enhanced Backend Implementation

```python
def _post_update_submission(self, id, state, done=False):
    if self._submission_cache_id == id:
        data = self._submission_cache
    else:
        self._submission_cache = data = (
            Submission.objects.filter(id=id)
            .values(
                "problem__is_public",
                "problem__is_organization_private",
                "contest_object__key",
                "contest_object__public_scoreboard",
                "user_id",
                "problem_id",
                "status",
                "language__key",
            )
            .get()
        )
        self._submission_cache_id = id

    # Enhanced live updates - send to multiple channels if enabled
    if getattr(settings, 'ENHANCED_LIVE_UPDATES', False):
        message_data = {
            "type": "done-submission" if done else "update-submission",
            "state": state,
            "id": id,
            "contest": data["contest_object__key"],
            "user": data["user_id"],
            "problem": data["problem_id"],
            "status": data["status"],
            "language": data["language__key"],
        }
        
        # Send to user's personal channel
        user_channel = f"submissions_user_{data['user_id']}_{self._get_channel_secret('user', data['user_id'])}"
        event.post(user_channel, {**message_data, "context": "user"})
        
        # Send to contest channel if in contest
        if data["contest_object__key"]:
            contest_channel = f"submissions_contest_{data['contest_object__key']}_{self._get_channel_secret('contest', data['contest_object__key'])}"
            event.post(contest_channel, {**message_data, "context": "contest"})
        
        # Send to problem channel
        problem_channel = f"submissions_problem_{data['problem_id']}_{self._get_channel_secret('problem', data['problem_id'])}"
        event.post(problem_channel, {**message_data, "context": "problem"})
        
        # Send to public channel (existing behavior with organization and contest fixes)
        should_send_to_public = (
            data["problem__is_public"] and
            not data.get("problem__is_organization_private", False)
        ) or (
            # Also send if contest has public scoreboard
            data.get("contest_object__public_scoreboard", False)
        )
        
        if should_send_to_public:
            event.post("submissions", message_data)
    else:
        # Original behavior - only public submissions (with organization and contest fixes)
        should_send_to_public = (
            data["problem__is_public"] and
            not data.get("problem__is_organization_private", False)
        ) or (
            # Also send if contest has public scoreboard
            data.get("contest_object__public_scoreboard", False)
        )
        
        if should_send_to_public:
            event.post(
                "submissions",
                {
                    "type": "done-submission" if done else "update-submission",
                    "state": state,
                    "id": id,
                    "contest": data["contest_object__key"],
                    "user": data["user_id"],
                    "problem": data["problem_id"],
                    "status": data["status"],
                    "language": data["language__key"],
                },
            )

def _get_channel_secret(self, channel_type, channel_id):
    """Generate HMAC secret for channel using shared secret key"""
    return hmac.new(
        utf8bytes(settings.EVENT_DAEMON_CHANNEL_KEY),
        f"{channel_type}_{channel_id}".encode(),
        hashlib.sha256
    ).hexdigest()[:16]
```

### 2. Enhanced Submission View Abstraction

#### Dynamic Channel Subscription System

```javascript
function load_dynamic_update_enhanced(last_msg, current_page, dynamic_channels) {
    var table = $('#submissions-table');
    var current_page_num = current_page || 1;
    var failed_submissions = new Set(); // Track submissions that failed to load
    
    // Calculate submission ID range for current page
    var first_submission_id = parseInt(table.find('>div:first-child').attr('id'));
    
    // Use channels provided by backend view logic
    var channels = dynamic_channels || ['submissions']; // Fallback to public channel
    
    var update_submission = function (message, force) {
        var id = message.id;
        
        // Skip if we've already failed to load this submission
        if (failed_submissions.has(id)) {
            return;
        }
        
        // Apply existing filters
        if (language_filter.length && 'language' in message &&
            language_filter.indexOf(message.language) == -1)
            return;
        if (status_filter.length && 'status' in message &&
            status_filter.indexOf(message.status) == -1)
            return;
        
        // Apply context-specific filters
        if (current_contest && message.contest != current_contest)
            return;
        if (dynamic_user_id && message.user != dynamic_user_id ||
            dynamic_problem_id && message.problem != dynamic_problem_id)
            return;
        
        var row = table.find('div#' + id);
        
        // Check if submission exists on current page
        if (row.length > 0) {
            // Update existing submission on current page
            update_existing_submission(row, id, force);
        } else {
            // Handle new submission based on page and ID
            handle_new_submission(id, message, force);
        }
    };
    
    var handle_new_submission = function(id, message, force) {
        // Only add to page 1 if submission ID is newer than first submission
        if (current_page_num === 1 && id > first_submission_id) {
            add_new_submission_to_page_one(id, message, force);
        }
        // For other pages, ignore new submissions
    };
    
    var add_new_submission_to_page_one = function(id, message, force) {
        var row = $('<div>', {id: id, 'class': 'submission-row'}).hide().prependTo(table);
        
        // Remove last submission if page is full
        if (table.find('>div').length > {{ paginator.per_page }}) {
            table.find('>div:last-child').hide('slow', function () {
                $(this).remove();
            });
        }
        
        update_existing_submission(row, id, force);
    };
    
    var update_existing_submission = function(row, id, force) {
        if (force || !doing_ajax) {
            if (!force) doing_ajax = true;
            $.ajax({
                url: '{{ url('submission_single_query') }}',
                data: {id: id, show_problem: show_problem}
            }).done(function (data) {
                var was_shown = row.is(':visible');
                row.html(data);
                register_time(row.find('.time-with-rel'));
                if (!was_shown) {
                    row.slideDown('slow');
                }
                if (!force)
                    setTimeout(function () {
                        doing_ajax = false;
                    }, 1000);
            }).fail(function (xhr) {
                console.log('Failed to update submission: ' + id);
                
                // If 404 or 403, mark as failed and don't retry
                if (xhr.status === 404 || xhr.status === 403) {
                    failed_submissions.add(id);
                    // Remove the row if it was newly created
                    if (!row.html()) {
                        row.remove();
                    }
                }
                
                if (!force) doing_ajax = false;
            });
        }
    };
    
    var $body = $(document.body);
    var receiver = new EventReceiver(
        "{{ EVENT_DAEMON_LOCATION }}",
        channels, last_msg, function (message) {
            if (message.type == 'update-submission') {
                if (message.state == 'test-case' && $body.hasClass('window-hidden'))
                    return;
                update_submission(message);
            } else if (message.type == 'done-submission') {
                update_submission(message, true);
                
                if (!statistics.length) return;
                if ($('body').hasClass('window-hidden'))
                    return stats_outdated = true;
                update_stats();
            }
        }
    );
    
    receiver.onwsclose = function (event) {
        if (event.code == 1001) {
            console.log('Navigated away');
            return;
        }
    };
    return receiver;
}
```

### 3. Enhanced SubmissionsListBase with Dynamic Channels

#### Extending Existing SubmissionsListBase Class

```python
class SubmissionsListBase(DiggPaginatorMixin, TitleMixin, ListView):
    # ... existing code ...
    
    def get_dynamic_channels(self):
        """
        Override in subclasses to specify which channels to subscribe to.
        Returns a list of channel names that the frontend should subscribe to.
        Similar to how dynamic_update works, but for channel subscription.
        """
        if not getattr(settings, 'ENHANCED_LIVE_UPDATES', False):
            return ['submissions']  # Legacy fallback
        
        if not self.request.user.is_authenticated:
            return ['submissions']  # Public channel only for anonymous users
        
        # Default implementation - override in subclasses
        return ['submissions']
    
    def _get_channel_secret(self, channel_type, channel_id):
        """Generate HMAC secret for channel using shared secret key"""
        return hmac.new(
            utf8bytes(settings.EVENT_DAEMON_CHANNEL_KEY),
            f"{channel_type}_{channel_id}".encode(),
            hashlib.sha256
        ).hexdigest()[:16]
    
    def _get_user_channel(self, user_id):
        """Get user-specific channel with secret"""
        return f"submissions_user_{user_id}_{self._get_channel_secret('user', user_id)}"
    
    def _get_contest_channel(self, contest_key):
        """Get contest-specific channel with secret"""
        return f"submissions_contest_{contest_key}_{self._get_channel_secret('contest', contest_key)}"
    
    def _get_problem_channel(self, problem_id):
        """Get problem-specific channel with secret"""
        return f"submissions_problem_{problem_id}_{self._get_channel_secret('problem', problem_id)}"
    
    def get_context_data(self, **kwargs):
        context = super(SubmissionsListBase, self).get_context_data(**kwargs)
        authenticated = self.request.user.is_authenticated
        
        # Enhanced live updates logic
        if getattr(settings, 'ENHANCED_LIVE_UPDATES', False):
            context["dynamic_update"] = True
            context["enhanced_live_updates_enabled"] = True
            context["dynamic_channels"] = self.get_dynamic_channels()
        else:
            # Original behavior
            context["dynamic_update"] = False  # Will be overridden by subclasses as needed
            context["enhanced_live_updates_enabled"] = False
            context["dynamic_channels"] = ['submissions']
        
        # ... rest of existing get_context_data code ...
        return context

# View-specific implementations with refined channel strategies
class AllSubmissions(InfinitePaginationMixin, GeneralSubmissions):
    def get_dynamic_channels(self):
        """Public submissions + user's own submissions"""
        channels = ['submissions']  # Public channel
        if self.request.user.is_authenticated:
            channels.append(self._get_user_channel(self.request.profile.id))
        return channels

class AllUserSubmissions(ConditionalUserTabMixin, UserMixin, GeneralSubmissions):
    def get_dynamic_channels(self):
        """Only target user's submissions"""
        return [self._get_user_channel(self.profile.id)]

class AllFriendSubmissions(LoginRequiredMixin, InfinitePaginationMixin, GeneralSubmissions):
    def get_dynamic_channels(self):
        """Same as AllUserSubmissions - user's own channel (friends filtered server-side)"""
        return [self._get_user_channel(self.request.profile.id)]

class ContestSubmissions(LoginRequiredMixin, ContestMixin, ForceContestMixin, SubmissionsListBase):
    def get_dynamic_channels(self):
        """Contest submissions with hidden subtasks consideration"""
        # Check if in hidden subtasks contest and not organizer
        if (hasattr(self, 'contest') and self.contest.format.has_hidden_subtasks
            and not self.contest.is_editable_by(self.request.user)):
            # Users can only see their own submissions in hidden subtasks contests
            return [self._get_user_channel(self.request.profile.id)]
        else:
            # Normal contest - subscribe to contest channel
            return [self._get_contest_channel(self.contest.key)]

class UserContestSubmissions(ForceContestMixin, UserProblemSubmissions):
    def get_dynamic_channels(self):
        """User-specific contest submissions - only user's channel for fewer updates"""
        return [self._get_user_channel(self.profile.id)]

class ProblemSubmissions(ProblemSubmissionsBase):
    def get_dynamic_channels(self):
        """Problem-specific submissions"""
        return [self._get_problem_channel(self.problem.id)]

class UserProblemSubmissions(ConditionalUserTabMixin, UserMixin, ProblemSubmissions):
    def get_dynamic_channels(self):
        """User + problem intersection - user channel has fewer updates"""
        return [self._get_user_channel(self.profile.id)]
```

## Configuration Settings

### Minimal Settings

```python
# settings.py

# Enhanced Live Updates Feature Toggle
ENHANCED_LIVE_UPDATES = getattr(settings, 'ENHANCED_LIVE_UPDATES', False)

# Single shared secret key for all channels
EVENT_DAEMON_CHANNEL_KEY = getattr(settings, 'EVENT_DAEMON_CHANNEL_KEY', 'default-channel-key')
```

### Environment Configuration

```bash
# .env or environment variables
ENHANCED_LIVE_UPDATES=true
EVENT_DAEMON_CHANNEL_KEY=your-secret-channel-key
```

## View-Specific Channel Architecture

### Channel Types and View Mapping

```
Channel: submissions
Purpose: Public submissions (not organization-private) OR contest submissions with public scoreboard
Views: AllSubmissions (fallback), legacy mode
Security: Open channel, includes contest submissions when contest.public_scoreboard=True

Channel: submissions_user_{user_id}_{hmac_hash}
Purpose: User's own submissions across all contexts
Views: AllSubmissions, AllUserSubmissions, AllFriendSubmissions, UserProblemSubmissions
Security: HMAC with shared secret, user_id

Channel: submissions_contest_{contest_key}_{hmac_hash}
Purpose: Contest submissions for participants/organizers
Views: ContestSubmissions, UserContestSubmissions
Security: HMAC with shared secret, contest_key

Channel: submissions_problem_{problem_id}_{hmac_hash}
Purpose: Problem-specific submissions
Views: ProblemSubmissions, UserProblemSubmissions
Security: HMAC with shared secret, problem_id
```

### Refined View-Specific Channel Strategy

| View Class | Channel Strategy | Reasoning |
|------------|-----------------|-----------|
| `AllSubmissions` | `submissions` + `submissions_user_{user_id}` | Public + user's own for comprehensive coverage |
| `AllUserSubmissions` | `submissions_user_{target_user_id}` | Only target user's submissions |
| `AllFriendSubmissions` | `submissions_user_{user_id}` | Same as user submissions (friends filtered server-side) |
| `ContestSubmissions` | `submissions_contest_{contest_key}` OR `submissions_user_{user_id}` | Contest channel, but user channel if hidden subtasks + not organizer |
| `UserContestSubmissions` | `submissions_user_{target_user_id}` | User channel only for fewer updates |
| `ProblemSubmissions` | `submissions_problem_{problem_id}` | Problem-specific submissions |
| `UserProblemSubmissions` | `submissions_user_{target_user_id}` | User channel for fewer updates than problem channel |

### Hidden Subtasks Contest Logic

```python
def get_dynamic_channels(self):
    """Contest submissions with hidden subtasks consideration"""
    if (self.contest.format.has_hidden_subtasks
        and not self.contest.is_editable_by(self.request.user)):
        # In hidden subtasks contests, non-organizers can only see their own submissions
        # So subscribe to user channel only
        return [self._get_user_channel(self.request.profile.id)]
    else:
        # Normal contest or organizer - subscribe to contest channel
        return [self._get_contest_channel(self.contest.key)]
```

### Security Model

- **Single shared secret**: `EVENT_DAEMON_CHANNEL_KEY` used for all channel HMAC generation
- **Context-specific channels**: Users only subscribe to channels they have access to
- **Server-side validation**: Django views enforce access control on AJAX requests
- **Failed submission tracking**: Frontend prevents retry loops on 404/403 responses

## Implementation Roadmap

### Phase 1: Backend Changes (Week 1)

1. **Modify `_post_update_submission()` method**
   - Add feature flag check: `if settings.ENHANCED_LIVE_UPDATES:`
   - Send to multiple channels when enabled
   - Add shared secret generation method
   - Maintain backward compatibility

2. **Update submission views**
   - Modify context data to enable dynamic updates on all pages when enhanced mode is on
   - Add user context with channel secrets for frontend subscription

### Phase 2: Frontend Changes (Week 2)

1. **Update submission list templates**
   - Add multi-channel subscription logic
   - Implement failed submission tracking
   - Handle pagination properly (ignore new submissions on pages != 1)

2. **Test channel access**
   - Verify users only subscribe to appropriate channels
   - Test graceful handling of 404/403 responses
   - Ensure no infinite retry loops

### Phase 3: Testing & Optimization (Week 3)

1. **Comprehensive testing**
   - Test with different user permission levels
   - Verify contest and private problem access control
   - Test pagination behavior on different pages

2. **Performance monitoring**
   - Monitor WebSocket message volume across channels
   - Check for any performance regressions
   - Optimize channel subscription logic if needed

## Benefits of Hybrid Approach

### Enhanced Security

- **Channel-based access control**: Users only subscribe to channels they should access
- **HMAC protection**: Channel names are cryptographically protected
- **Server-side authority**: All access control enforced by Django views
- **Graceful failure**: 404/403 responses handled properly

### Better User Experience

- **Targeted updates**: Users get updates for submissions they care about
- **Reduced noise**: No irrelevant updates from inaccessible submissions
- **All pages work**: Dynamic updates enabled on all pages when enhanced mode is on
- **Smart pagination**: New submissions only added to page 1

### Simplified Configuration

- **Two settings only**: `ENHANCED_LIVE_UPDATES` and `EVENT_DAEMON_CHANNEL_KEY`
- **Shared secret**: Single key for all channel types
- **Easy deployment**: Simple on/off toggle with one secret key

## Edge Cases Handled

### Organization-Private Problem Security

1. **Current vulnerability**: Problems with `is_public=True` and `is_organization_private=True` send WebSocket updates to all users
2. **Security fix**: Only send to public channel if `is_public=True` AND `is_organization_private=False`
3. **Organization access**: Users in the organization get updates via user/contest/problem channels
4. **Information leakage prevention**: Non-organization users don't receive updates about organization-private submissions

### Public Scoreboard Contest Support

1. **Contest visibility**: When `contest.public_scoreboard=True`, submissions should be visible to everyone
2. **Public channel inclusion**: Contest submissions with public scoreboard are sent to public channel regardless of problem privacy
3. **Access control**: Frontend can access these submissions because contest scoreboard is public
4. **Live updates**: Everyone can watch submission updates for contests with public scoreboards

### Enhanced View-Specific Access Control

1. **AllSubmissions**: Public + user channels for comprehensive coverage
2. **AllUserSubmissions**: Target user channel only for focused updates
3. **AllFriendSubmissions**: User's own channel (same as user submissions, friends filtered server-side)
4. **ContestSubmissions**: Contest channel OR user channel (if hidden subtasks + not organizer)
5. **UserContestSubmissions**: User channel only for fewer updates than contest channel
6. **ProblemSubmissions**: Problem channel for problem-specific updates
7. **UserProblemSubmissions**: User channel only for fewer updates than problem channel
8. **Failed access**: Submissions that can't be loaded are tracked and ignored

### Dynamic Channel Abstraction Benefits

1. **Simplified Frontend**: Frontend just uses `dynamic_channels` array from context
2. **Backend Control**: Each view determines its optimal channel subscription strategy
3. **Performance Optimization**: Users only get relevant updates based on view context
4. **Hidden Subtasks Support**: Automatic handling of restricted contest visibility
5. **Consistent API**: Similar to existing `dynamic_update` pattern in codebase

### Pagination Behavior

1. **Page 1**: New submissions are added at the top, old ones removed from bottom
2. **Other pages**: Existing submissions are updated, new submissions are ignored
3. **Failed loads**: Submissions that can't be loaded are marked and ignored

### Channel Subscription Logic

1. **Context-aware**: Only subscribe to relevant channels based on page context
2. **Permission-based**: Channel secrets only provided for accessible contexts
3. **Fallback**: Always includes public channel for backward compatibility

This hybrid approach provides the security and targeting benefits of multiple channels while maintaining simplicity with minimal configuration settings.