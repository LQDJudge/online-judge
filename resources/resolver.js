function Resolver(problem_sub, sub_frozen, problems, users) {
	this.problem_sub = problem_sub;
	this.sub_frozen = sub_frozen;
	this.problems = problems;
	this.users = users;
	this.frozen_seconds = 200;
	this.operations = [];
	this.frozen_op = 0;
	this.isshow = [];
	this.total_points = {};
	this.delay = false;

	for (let problem in this.problems) {
		this.total_points[problem] = 0;
		for (let i in this.problems[problem]) {
			this.total_points[problem] += this.problems[problem][i];
		}
		this.total_points[problem] = round2(this.total_points[problem]);
	}
}

function round2(num) {
	return Math.round(num * 100) / 100;
}

Resolver.prototype.status = function (problem) {
	if (problem.old_verdict === 'NA' && problem.new_verdict === 'NA') {
		return 'untouched';
	} else if (problem.old_verdict === 'AC') {
		return 'ac';
	} else if (problem.old_verdict === 'PA' && problem.new_verdict === 'NA') {
		return 'partially';
	} else if (problem.new_verdict === 'NA' && problem.old_verdict === 'WA') {
		return 'failed';
	} else {
		return "frozen";
	}
}

Resolver.prototype.substatus = function (problem, subproblem) {
	if (problem.old_verdict === 'NA' && problem.new_verdict === 'NA') {
		return 'untouched';
	} else if (problem[subproblem].old_verdict === 'AC') {
		return 'ac';
	} else if (problem[subproblem].old_verdict === 'PA' && problem.new_verdict === 'NA') {
		return 'partially';
	} else if (problem[subproblem].old_verdict === 'WA' && problem.new_verdict === 'NA') {
		return 'failed';
	}
	else {
		return 'frozen';
	}
}

Resolver.prototype.pointstatus = function (point, problem, sub) {
	if (sub === undefined) {
		if (point === this.total_points[problem]) return 'AC';
		if (point === 0) return 'WA';
		return 'PA';
	}
	if (point === this.problems[problem][sub]) return 'AC';
	if (point === 0) return 'WA';
	return 'PA';
}

Resolver.prototype.calcOperations = function () {
	this.rank = {};
	this.users_cnt = Object.keys(this.users).length;
	for (let id = 1; id <= this.users_cnt; id++) {
		this.rank[id] = {
			'user_id': id,
			'score': 0,
			'rank_show': -1,
			'last_submission': this.users[id].last_submission,
		};
		this.rank[id].problem = {}
		for (let i = 1; i <= this.problem_sub.length; i++) {
			this.rank[id].problem[i] = {
				'old_point': 0,
				'new_point': 0,
				'old_verdict': 'NA',
				'new_verdict': 'NA',
			}
			for (let j = 1; j <= this.problem_sub[i - 1]; j++) {
				this.rank[id].problem[i][j] = {
					'old_point': 0,
					'new_point': 0,
					'old_verdict': 'NA',
					'new_verdict': 'NA'
				};
			}
		}
		for (let problemid in this.users[id].problems) {
			for (let j = 1; j <= this.problem_sub[problemid - 1]; j++) {
				if (j < this.sub_frozen[problemid - 1]) {
					this.rank[id].problem[problemid][j].old_point = this.users[id].problems[problemid].frozen_points[j];
					this.rank[id].problem[problemid].old_point += this.rank[id].problem[problemid][j].old_point;
					this.rank[id].problem[problemid][j].old_verdict = this.pointstatus(this.users[id].problems[problemid].frozen_points[j], problemid, j);
					this.rank[id].problem[problemid].old_point = round2(this.rank[id].problem[problemid].old_point)
					if (this.users[id].problems[problemid].points[j] !== -1) {
						this.rank[id].problem[problemid][j].new_point = this.users[id].problems[problemid].points[j];
						this.rank[id].problem[problemid].new_point += this.rank[id].problem[problemid][j].new_point;
						this.rank[id].problem[problemid].new_point = round2(this.rank[id].problem[problemid].new_point)
						this.rank[id].problem[problemid][j].new_verdict = this.pointstatus(this.users[id].problems[problemid].points[j], problemid, j);
					}
				}
			}
			this.rank[id].problem[problemid].old_verdict = this.pointstatus(this.rank[id].problem[problemid].old_point, problemid);
			this.rank[id].score += this.rank[id].problem[problemid].old_point;
			this.rank[id].score = round2(this.rank[id].score)
			if (this.users[id].problems[problemid].points[1] !== -1) {
				this.rank[id].problem[problemid].new_verdict = this.pointstatus(this.rank[id].problem[problemid].new_point, problemid);
			}
		}
	}
	this.rank_frozen = $.extend(true, [], this.rank);
	const uids = Object.keys(this.rank);
	this.rankarr = [];
	for (let key in uids) {
		this.rankarr.push(this.rank[uids[key]]);
	}
	this.rankarr.sort(function (a, b) {
		if (a.score !== b.score) {
			return (b.score - a.score);
		} else {
			return (a.last_submission - b.last_submission);
		}
	});

	for (let i = 0; i < this.rankarr.length; i++) {
		this.rankarr[i].rank_show = i + 1;
		this.rank[this.rankarr[i].user_id].rank_show = i + 1;
		this.rank_frozen[this.rankarr[i].user_id].rank_show = i + 1;
	}
	console.log(this.rank_frozen);
	for (let i = this.rankarr.length - 1; i >= 0; i--) {
		let flag = true;
		while (flag) {
			flag = false;
			for (let j = 1; j <= this.problem_sub.length; j++) {
				if (this.status(this.rankarr[i].problem[j]) === 'frozen') {
					this.frozen_op = true;
					flag = true;
					for (let sub = 1; sub < this.sub_frozen[j - 1]; sub++) {
						if (this.rankarr[i].problem[j][sub].old_verdict === 'AC') continue;
						var op = {
							id: this.operations.length,
							type: 'sub',
							frozen: 'no',
							user_id: this.rankarr[i].user_id,
							problem_index: j,
							problem_sub: sub,
							old_point: this.rankarr[i].problem[j][sub].old_point,
							new_point: this.rankarr[i].problem[j][sub].new_point,
							old_verdict: this.rankarr[i].problem[j][sub].old_verdict,
							new_verdict: this.rankarr[i].problem[j][sub].new_verdict,
						};
						let tmp = this.rankarr[i];
						tmp.problem[j][sub].old_point = tmp.problem[j][sub].new_point;
						tmp.problem[j][sub].new_point = 0;
						tmp.problem[j][sub].old_verdict = tmp.problem[j][sub].new_verdict;
						tmp.problem[j][sub].new_verdict = 'NA';
						this.operations.push(op);
					}
					var op = {
						id: this.operations.length,
						type: 'problem',
						frozen: 'no',
						user_id: this.rankarr[i].user_id,
						problem_index: j,
						old_point: this.rankarr[i].problem[j].old_point,
						new_point: this.rankarr[i].problem[j].new_point,
						old_verdict: this.rankarr[i].problem[j].old_verdict,
						new_verdict: this.rankarr[i].problem[j].new_verdict,
						old_rank: i + 1,
						new_rank: -1,
					};
					let tmp = this.rankarr[i];
					if (tmp.problem[j].new_point > tmp.problem[j].old_point) {
						tmp.score += tmp.problem[j].new_point - tmp.problem[j].old_point;
						tmp.score = round2(tmp.score);
					}
					tmp.problem[j].old_point = tmp.problem[j].new_point;
					tmp.problem[j].new_point = 0;
					tmp.problem[j].old_verdict = tmp.problem[j].new_verdict;
					tmp.problem[j].new_verdict = 'NA';
					let k = i - 1;
					while (k >= 0 && this.rankarr[k].score < tmp.score) {
						tmp.rank_show--;
						this.rankarr[k].rank_show++;
						this.rankarr[k + 1] = this.rankarr[k];
						k--;
					}
					this.rankarr[k + 1] = tmp;
					op.new_rank = k + 2;
					this.operations.push(op);
					break;
				}
			}
		}
	}
	this.check = [];
	for (let i = 1; i <= this.users_cnt; i++) {
		const usercheck = [];
		for (let j = 1; j <= this.problem_sub.length; j++) {
			const cc = [];
			for (let k = 1; k <= this.sub_frozen[j - 1] - 1; k++) {
				cc.push(k);
			}
			usercheck.push(cc);
		}
		this.check.push(usercheck);
	}
}

Resolver.prototype.showrank = function () {
	for (let rankid = this.rankarr.length - 1; rankid >= 0; rankid--) {
		if (this.isshow.indexOf(this.rankarr[rankid].user_id) !== -1) continue;
		let ok = true;
		for (let problemid in this.users[this.rankarr[rankid].user_id].problems) {
			for (let sub = 1; sub <= this.problem_sub[problemid - 1]; sub++) {
				if (this.check[this.rankarr[rankid].user_id - 1][problemid - 1].indexOf(sub) === -1) {
					ok = false;
				}
			}
		}
		if (ok) {
			const op = {
				id: this.operations.length,
				type: 'show',
				user_id: this.rankarr[rankid].user_id,
			};
			this.delay = true;
			this.isshow.push(this.rankarr[rankid].user_id);
			this.operations.push(op);
			return true;
		} else {
			return false;
		}
	}
	return false;
}

Resolver.prototype.next_operation = function () {
	if (this.delay) {
		const op = {
			id: this.operations.length,
			type: 'delay',
		};
		this.delay = false;
		this.operations.push(op);
		return true;
	}
	const isshowrank = this.showrank();
	if (isshowrank === true) return true;
	for (let i = this.rankarr.length - 1; i >= 0; i--) {
		for (let problemid = 1; problemid <= this.problem_sub.length; problemid++) {
			let ok = false;
			const id = this.rankarr[i].user_id;
			for (let cc in this.users[id].problems) {
				if (cc === problemid) ok = true;
			}
			if (ok === false) {
				continue;
			}
			for (let sub = this.sub_frozen[problemid - 1]; sub <= this.problem_sub[problemid - 1]; sub++) {
				if (this.check[this.rankarr[i].user_id - 1][problemid - 1].indexOf(sub) === -1) {
					this.operation(i, problemid, sub);
					return true;
				}
			}
		}
	}
	return false;
}

Resolver.prototype.operation = function (rankid, problemid, sub) {
	console.log("Arr lengt",this.rankarr.length)
	console.log("rankid", rankid)
	console.log(this.rankarr)
	const id = this.rankarr[rankid].user_id;
	if (this.check[this.rankarr[rankid].user_id - 1][problemid - 1].indexOf(sub) !== -1) return false;
	this.check[this.rankarr[rankid].user_id - 1][problemid - 1].push(sub);
	this.rankarr[rankid].problem[problemid][sub].new_point = this.users[id].problems[problemid].points[sub];
	this.rankarr[rankid].problem[problemid][sub].new_verdict = this.pointstatus(this.rankarr[rankid].problem[problemid][sub].new_point, problemid, sub);
	this.rankarr[rankid].problem[problemid].new_point =
		this.rankarr[rankid].problem[problemid].old_point + this.rankarr[rankid].problem[problemid][sub].new_point - this.rankarr[rankid].problem[problemid][sub].old_point;
	this.rankarr[rankid].problem[problemid].new_verdict = this.pointstatus(this.rankarr[rankid].problem[problemid].new_point, problemid);
	this.rankarr[rankid].problem[problemid].new_point = round2(this.rankarr[rankid].problem[problemid].new_point)
	const op = {
		id: this.operations.length,
		type: 'sub',
		frozen: 'ok',
		user_id: this.rankarr[rankid].user_id,
		problem_index: problemid,
		problem_sub: sub,
		old_point: this.rankarr[rankid].problem[problemid][sub].old_point,
		new_point: this.rankarr[rankid].problem[problemid][sub].new_point,
		old_verdict: this.rankarr[rankid].problem[problemid][sub].old_verdict,
		new_verdict: this.rankarr[rankid].problem[problemid][sub].new_verdict,
	};
	const tmp = this.rankarr[rankid];
	tmp.problem[problemid][sub].old_point = tmp.problem[problemid][sub].new_point;
	tmp.problem[problemid][sub].new_point = 0;
	tmp.problem[problemid][sub].old_verdict = tmp.problem[problemid][sub].new_verdict;
	tmp.problem[problemid][sub].new_verdict = 'NA';
	this.operations.push(op);
	const op1 = {
		id: this.operations.length,
		type: 'problem',
		frozen: 'ok',
		user_id: this.rankarr[rankid].user_id,
		problem_index: problemid,
		old_point: this.rankarr[rankid].problem[problemid].old_point,
		new_point: this.rankarr[rankid].problem[problemid].new_point,
		old_verdict: this.rankarr[rankid].problem[problemid].old_verdict,
		new_verdict: this.rankarr[rankid].problem[problemid].new_verdict,
		old_rank: rankid + 1,
		new_rank: -1,
	};
	if (tmp.problem[problemid].new_point > tmp.problem[problemid].old_point) {
		tmp.score += tmp.problem[problemid].new_point - tmp.problem[problemid].old_point;
		tmp.score = round2(tmp.score)
	}
	tmp.problem[problemid].old_point = tmp.problem[problemid].new_point;
	tmp.problem[problemid].new_point = 0;
	tmp.problem[problemid].old_verdict = tmp.problem[problemid].new_verdict;
	tmp.problem[problemid].new_verdict = 'NA';
	let k = rankid - 1;
	while (k >= 0 && (this.rankarr[k].score < tmp.score || (this.rankarr[k].score === tmp.score && this.rankarr[k].last_submission > tmp.last_submission))) {
		tmp.rank_show--;
		this.rankarr[k].rank_show++;
		this.rankarr[k + 1] = this.rankarr[k];
		k--;
	}
	this.rankarr[k + 1] = tmp;
	op1.new_rank = k + 2;
	this.operations.push(op1);
	return true;
}