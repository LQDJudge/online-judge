/**
 * ContestResolver — universal resolver for frozen scoreboards and hidden subtasks.
 *
 * Input `data` shape (from backend):
 *   mode: 'frozen' | 'subtask'
 *   problems: [{order, label, code, max_points}, ...]
 *   users: [{username, display_name, school, problems, frozen_total, final_total, cumtime}, ...]
 *
 * In subtask mode, subtasks are flattened into individual problems during init,
 * so the resolver logic only ever deals with simple {frozen, final} entries.
 */

class ContestResolver {
  constructor(data) {
    if (data.mode === 'subtask') {
      data = ContestResolver._flattenSubtasks(data);
    }
    this.showCumtime = data.show_cumtime !== false;
    this.problems = data.problems;
    this.rawUsers = data.users;
    this.users = [];
    this.ranking = [];
    this.operations = [];
    this.currentOp = -1;
    this.autoPlaying = false;
    this.autoTimer = null;

    this._buildUsers();
    this._buildInitialRanking();
    this._buildOperations();
  }

  // ------------------------------------------------------------------
  // Flatten subtasks into individual problems
  // ------------------------------------------------------------------
  static _flattenSubtasks(data) {
    // Determine subtask structure per problem from any user that has data
    var subtaskInfo = {};
    for (var ui = 0; ui < data.users.length; ui++) {
      for (var pi = 0; pi < data.problems.length; pi++) {
        var key = String(data.problems[pi].order);
        var pd = data.users[ui].problems[key];
        if (pd && pd.subtasks && !subtaskInfo[key]) {
          subtaskInfo[key] = pd.subtasks.map(function (s) {
            return { max: s.max, hidden: s.hidden };
          });
        }
      }
    }

    var flatProblems = [];
    var mapping = []; // each: { originalOrder, indices: [si, ...] }
    var flatOrder = 0;

    for (var pi = 0; pi < data.problems.length; pi++) {
      var p = data.problems[pi];
      var pkey = String(p.order);
      var subs = subtaskInfo[pkey];
      if (subs) {
        var revealed = [], hidden = [];
        for (var si = 0; si < subs.length; si++) {
          if (subs[si].hidden) hidden.push(si);
          else revealed.push(si);
        }

        var hasHidden = hidden.length > 0;
        var mergedFlatOrder = -1;

        // One merged column for all revealed (non-hidden) subtasks
        if (revealed.length > 0) {
          var mergedMax = 0;
          for (var ri = 0; ri < revealed.length; ri++) mergedMax += subs[revealed[ri]].max;
          mergedMax = Math.round(mergedMax * 100) / 100;

          // Total max across ALL subtasks (for verdict after full merge)
          var totalMax = 0;
          for (var ti = 0; ti < subs.length; ti++) totalMax += subs[ti].max;
          totalMax = Math.round(totalMax * 100) / 100;

          // Build sublabel: "1-3" if contiguous, "1,3,5" otherwise
          var sublabel = null;
          if (hasHidden) {
            var nums = revealed.map(function (i) { return i + 1; });
            if (nums.length === 1) {
              sublabel = String(nums[0]);
            } else if (nums[nums.length - 1] - nums[0] === nums.length - 1) {
              sublabel = nums[0] + '-' + nums[nums.length - 1];
            } else {
              sublabel = nums.join(',');
            }
          }

          mergedFlatOrder = flatOrder;
          flatProblems.push({
            order: flatOrder,
            label: hasHidden ? p.label + '(' + sublabel + ')' : p.label,
            code: p.code,
            max_points: mergedMax,
            mergedMaxTotal: totalMax,
            group: hasHidden ? p.label : null,
            sublabel: sublabel,
          });
          mapping.push({ originalOrder: p.order, indices: revealed });
          flatOrder++;
        }

        // Individual columns for hidden subtasks
        for (var hi = 0; hi < hidden.length; hi++) {
          var hsi = hidden[hi];
          flatProblems.push({
            order: flatOrder,
            label: p.label + (hsi + 1),
            code: p.code,
            max_points: subs[hsi].max,
            group: p.label,
            sublabel: String(hsi + 1),
            mergedOrder: mergedFlatOrder >= 0 ? mergedFlatOrder : undefined,
          });
          mapping.push({ originalOrder: p.order, indices: [hsi] });
          flatOrder++;
        }
      } else {
        flatProblems.push({
          order: flatOrder,
          label: p.label,
          code: p.code,
          max_points: p.max_points,
          group: null,
          sublabel: null,
        });
        mapping.push({ originalOrder: p.order, indices: null });
        flatOrder++;
      }
    }

    var flatUsers = data.users.map(function (u) {
      var problems = {};
      var frozenTotal = 0, finalTotal = 0;
      for (var i = 0; i < mapping.length; i++) {
        var m = mapping[i];
        var origPd = u.problems[String(m.originalOrder)];
        var frozen = 0, finalVal = 0;

        if (m.indices === null) {
          frozen = origPd ? origPd.frozen : 0;
          finalVal = origPd ? origPd.final : 0;
        } else {
          for (var si = 0; si < m.indices.length; si++) {
            var s = origPd && origPd.subtasks ? origPd.subtasks[m.indices[si]] : null;
            frozen += s ? s.frozen : 0;
            finalVal += s ? s.final : 0;
          }
          frozen = Math.round(frozen * 100) / 100;
          finalVal = Math.round(finalVal * 100) / 100;
        }

        problems[String(flatProblems[i].order)] = { frozen: frozen, final: finalVal };
        frozenTotal += frozen;
        finalTotal += finalVal;
      }
      return {
        username: u.username,
        display_name: u.display_name,
        school: u.school,
        css_class: u.css_class,
        problems: problems,
        frozen_total: Math.round(frozenTotal * 100) / 100,
        final_total: Math.round(finalTotal * 100) / 100,
        cumtime: u.cumtime,
      };
    });

    return {
      contest_name: data.contest_name,
      mode: 'frozen',
      show_cumtime: data.show_cumtime,
      problems: flatProblems,
      users: flatUsers,
    };
  }

  // ------------------------------------------------------------------
  // Initialisation
  // ------------------------------------------------------------------
  _buildUsers() {
    this.users = this.rawUsers.map(function (u, idx) {
      var problems = {};
      for (var pi = 0; pi < this.problems.length; pi++) {
        var p = this.problems[pi];
        var key = String(p.order);
        var pd = u.problems[key];
        if (!pd) {
          problems[key] = { frozen: 0, final: 0, current: 0, revealed: true };
          continue;
        }
        var hasDiff = Math.abs(pd.frozen - pd.final) > 1e-9;
        problems[key] = {
          frozen: pd.frozen,
          final: pd.final,
          current: pd.frozen,
          revealed: !hasDiff,
        };
      }
      return {
        id: idx,
        username: u.username,
        displayName: u.display_name,
        school: u.school,
        cssClass: u.css_class || '',
        problems: problems,
        frozenTotal: u.frozen_total,
        finalTotal: u.final_total,
        currentTotal: u.frozen_total,
        cumtime: u.cumtime,
      };
    }.bind(this));
  }

  _buildInitialRanking() {
    this.ranking = this.users.map(function (_, i) { return i; });
    this.ranking.sort(function (a, b) {
      var ua = this.users[a], ub = this.users[b];
      if (Math.abs(ub.currentTotal - ua.currentTotal) > 1e-9)
        return ub.currentTotal - ua.currentTotal;
      return ua.cumtime - ub.cumtime;
    }.bind(this));
  }

  _buildOperations() {
    var self = this;
    // Simulation copy
    var simUsers = this.users.map(function (u) {
      var problems = {};
      for (var pi = 0; pi < self.problems.length; pi++) {
        var key = String(self.problems[pi].order);
        var orig = self.users[u.id].problems[key];
        problems[key] = { current: orig.frozen, final: orig.final, revealed: orig.revealed };
      }
      return { id: u.id, currentTotal: u.currentTotal, cumtime: u.cumtime, problems: problems };
    });

    var simRanking = this.ranking.slice();

    var getSimRank = function (userId) { return simRanking.indexOf(userId); };

    var reSort = function () {
      simRanking.sort(function (a, b) {
        var ua = simUsers[a], ub = simUsers[b];
        if (Math.abs(ub.currentTotal - ua.currentTotal) > 1e-9)
          return ub.currentTotal - ua.currentTotal;
        return ua.cumtime - ub.cumtime;
      });
    };

    var concluded = {};
    var concludeReady = function () {
      var ci = simRanking.length - 1;
      while (ci >= 0) {
        var uid = simRanking[ci];
        if (concluded[uid]) { ci--; continue; }
        var su = simUsers[uid];
        var allDone = self.problems.every(function (p) {
          return su.problems[String(p.order)].revealed;
        });
        if (allDone) {
          concluded[uid] = true;
          self.operations.push({ type: 'show-overlay', userId: uid, rank: ci + 1 });
          ci--;
        } else {
          break;
        }
      }
    };

    var ri = simRanking.length - 1;
    while (ri >= 0) {
      var userId = simRanking[ri];
      if (concluded[userId]) { ri--; continue; }
      var su = simUsers[userId];

      var foundUnrevealed = false;
      for (var pi = 0; pi < this.problems.length; pi++) {
        var p = this.problems[pi];
        var key = String(p.order);
        var sp = su.problems[key];
        if (sp.revealed) continue;

        foundUnrevealed = true;
        var oldRank = getSimRank(userId);
        var oldCurrent = sp.current;
        sp.current = sp.final;
        sp.revealed = true;

        su.currentTotal += sp.final - oldCurrent;
        su.currentTotal = Math.round(su.currentTotal * 100) / 100;

        reSort();
        var newRank = getSimRank(userId);
        var verdict = self._getVerdict(oldCurrent, sp.final, p.max_points);

        this.operations.push({
          type: 'reveal',
          userId: userId,
          problemOrder: p.order,
          frozenScore: oldCurrent,
          finalScore: sp.final,
          maxPoints: p.max_points,
          oldRank: oldRank + 1,
          newRank: newRank + 1,
          newTotal: su.currentTotal,
          verdict: verdict,
        });

        concludeReady();
        ri = simRanking.length - 1;
        while (ri >= 0 && concluded[simRanking[ri]]) ri--;
        break;
      }

      if (!foundUnrevealed) {
        concludeReady();
        ri--;
        while (ri >= 0 && concluded[simRanking[ri]]) ri--;
      }
    }

    concludeReady();
  }

  _getVerdict(frozen, final, maxPoints) {
    if (Math.abs(final - maxPoints) < 1e-9) return 'ac';
    if (final > frozen + 1e-9) return 'improved';
    if (final > 0) return 'partial';
    return 'wrong';
  }

  // ------------------------------------------------------------------
  // Public API
  // ------------------------------------------------------------------
  next() {
    if (this.currentOp >= this.operations.length - 1) return null;
    this.currentOp++;
    var op = this.operations[this.currentOp];
    this._applyOperation(op);
    return op;
  }

  hasNext() {
    return this.currentOp < this.operations.length - 1;
  }

  revealCell(userId, problemOrder) {
    var insertAt = this.currentOp + 1;
    for (var i = insertAt; i < this.operations.length; i++) {
      var op = this.operations[i];
      if (op.userId === userId && op.problemOrder === problemOrder) {
        this.operations.splice(i, 1);
        this.operations.splice(insertAt, 0, op);
        insertAt++;
      }
    }
  }

  stopAutoPlay() {
    this.autoPlaying = false;
    if (this.autoTimer) {
      clearTimeout(this.autoTimer);
      this.autoTimer = null;
    }
  }

  // ------------------------------------------------------------------
  // Internal
  // ------------------------------------------------------------------
  _applyOperation(op) {
    if (op.type === 'reveal') {
      var user = this.users[op.userId];
      var prob = user.problems[String(op.problemOrder)];
      prob.current = op.finalScore;
      prob.revealed = true;
      user.currentTotal = op.newTotal;

      this.ranking.sort(function (a, b) {
        var ua = this.users[a], ub = this.users[b];
        if (Math.abs(ub.currentTotal - ua.currentTotal) > 1e-9)
          return ub.currentTotal - ua.currentTotal;
        return ua.cumtime - ub.cumtime;
      }.bind(this));
    }
  }

  getRank(userId) {
    return this.ranking.indexOf(userId) + 1;
  }

  getDisplayRanks() {
    var ranks = {};
    var displayRank = 1;
    for (var i = 0; i < this.ranking.length; i++) {
      var uid = this.ranking[i];
      var u = this.users[uid];
      if (i > 0) {
        var prev = this.users[this.ranking[i - 1]];
        if (Math.abs(u.currentTotal - prev.currentTotal) > 1e-9 ||
            Math.abs(u.cumtime - prev.cumtime) > 1e-9) {
          displayRank = i + 1;
        }
      }
      ranks[uid] = displayRank;
    }
    return ranks;
  }

  static formatCumtime(seconds) {
    if (!seconds) return '';
    var h = Math.floor(seconds / 3600);
    var m = Math.floor((seconds % 3600) / 60);
    if (h > 0) return h + 'h ' + m + 'm';
    return m + 'm';
  }
}
