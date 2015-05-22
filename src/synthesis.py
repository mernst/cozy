#!/usr/bin/env python2

import datetime
import itertools
import math
import random
import sys
from collections import defaultdict
from z3 import *

import predicates
import plans

class SolverContext:

    def __init__(self, varNames, fieldNames, cost_model, assumptions=()):
        self.varNames = varNames
        self.fieldNames = fieldNames
        self.z3ctx = Context()
        self.z3solver = SolverFor("QF_LIA", ctx=self.z3ctx)
        for a in assumptions:
            self.z3solver.add(a.toZ3(self.z3ctx))
        self.cost_model = cost_model

    def cost(self, plan):
        if not hasattr(plan, "_cost"):
            plan._cost = self.cost_model(plan)
            # This small bump is to ensure we favor simpler plans
            plan._cost += plan.size() / 10000.0
        return plan._cost

    def synthesizePlansByEnumeration(self, query, sort_field=None, maxSize=1000):
        examples = []
        query = query.toNNF()

        dumbestPlan = plans.Filter(plans.AllWhere(predicates.Bool(True)), query)
        self.bestCost = self.cost(dumbestPlan) # cost of best valid plan found so far
        self.bestPlans = set() # set of valid plans with cost == self.bestCost
        self.productive = False # was progress made this iteration
        yield dumbestPlan

        while True:
            # print "starting synthesis using", len(examples), "examples"
            for responseType, response in self._synthesizePlansByEnumeration(query, sort_field, maxSize, examples):
                if responseType == "counterexample":
                    example, plan = response
                    print "found counterexample", example, "\n\tfor", plan
                    if example in examples:
                        raise Exception("error: already saw counterexample!")
                    examples.append(example)
                    break
                elif responseType == "validPlan":
                    yield response
                elif responseType == "stop":
                    return

    def _synthesizePlansByEnumeration(self, query, sort_field, maxSize, examples):
        """note: query should be in NNF"""

        def outputvector(predicate):
            correct_sorting = True
            if isinstance(predicate, plans.Plan):
                if sort_field is not None:
                    correct_sorting = predicate.isSortedBy(sort_field)
                predicate = predicate.toPredicate()
            if not hasattr(predicate, "_outputvector") or len(predicate._outputvector) != len(examples) + 1:
                vec = [predicate.eval(dict(itertools.chain(zip(self.varNames, vs), zip(self.fieldNames, fs)))) for fs,vs,_ in examples]
                if sort_field is not None:
                    vec.append(correct_sorting)
                predicate._outputvector = tuple(vec)
            return predicate._outputvector

        def stupid(plan):
            if type(plan) is plans.Filter and type(plan.plan) is plans.Filter:
                return True
            if type(plan) in [plans.HashLookup, plans.BinarySearch, plans.Filter]:
                return outputvector(plan) == outputvector(plan.plan) or stupid(plan.plan)
            if type(plan) in [plans.Intersect, plans.Union, plans.Concat]:
                return (outputvector(plan) == outputvector(plan.plan1) or
                    outputvector(plan) == outputvector(plan.plan2) or
                    outputvector(plan.plan1) == outputvector(plan.plan2) or
                    plan.plan1 <= plan.plan2 or
                    stupid(plan.plan1) or stupid(plan.plan2))

        def isValid(plan):
            """returns True, False, or a new counterexample"""
            assert len(outputvector(plan)) == len(queryVector)
            if outputvector(plan) != queryVector:
                return False

            result = False
            s = self.z3solver
            s.push()
            try:
                s.add(plan.toPredicate().toZ3(self.z3ctx) != query.toZ3(self.z3ctx))
            except Exception as e:
                print plan, e, plan.toPredicate()
                raise e
            if str(s.check()) == 'unsat':
                result = True
            else:
                m = s.model()
                result = (
                    [int(str(m[Int(f, self.z3ctx)] or 0)) for f in self.fieldNames],
                    [int(str(m[Int(v, self.z3ctx)] or 0)) for v in self.varNames],
                    plan.isSortedBy(sort_field) if sort_field is not None else True)
            s.pop()

            return result

        def consider(plan, size):
            assert plan.size() == size
            if not plan.wellFormed(self.z3ctx, self.z3solver, self.fieldNames, self.varNames) or stupid(plan):
                return None, None
            x = isValid(plan)
            cost = self.cost(plan)

            # too expensive? it can't possibly be part of a great plan!
            if self.bestCost is not None and cost > self.bestCost:
                return None, None

            if x is True:
                self.productive = "new valid plan"
                if cost < self.bestCost:
                    self.bestCost = cost
                    self.bestPlans = set()
                self.bestPlans.add(plan)
                # evict big cached items
                for val, p in cache.items():
                    if self.cost(p) > cost:
                        del cache[val]
                for i in xrange(size + 1):
                    plansOfSize[i] = [p for p in plansOfSize[i] if self.cost(p) <= cost]
                return "validPlan", plan
            elif x is False:
                vec = outputvector(plan)
                old_plan = cache.get(vec)

                # new possibility
                if old_plan is None:
                    cache[vec] = plan
                    plansOfSize[size].append(plan)
                    self.productive = "new possibility: {}".format("".join(str(int(v)) for v in vec))

                # better than previous options
                elif cost < self.cost(old_plan):
                    cache[vec] = plan
                    for i in xrange(size + 1):
                        plansOfSize[i] = [p for p in plansOfSize[i] if outputvector(p) != vec]
                    plansOfSize[size].append(plan)
                    if any(p.contains_subtree(plan) for p in self.bestPlans):
                        self.productive = "better option for {}".format("".join(str(int(v)) for v in vec))

                # as good as previous options
                elif cost == self.cost(old_plan):
                    plansOfSize[size].append(plan)

                return None, None
            else:
                # x is new example!
                self.productive = "new counterexample"
                return "counterexample", (x, plan)

        def registerExp(e):
            vec = outputvector(e)
            if vec in ecache:
                return
            ecache[vec] = e
            exprsOfSize[e.size()].append(e)

        def pickToSum(groupedBySize1, groupedBySize2, sum):
            return ((x1, x2) for split in xrange(1, sum-1) for x1 in groupedBySize1[split] for x2 in groupedBySize2[sum-split-1])

        queryVector = outputvector(query)
        comps = set(query.comparisons())
        for a, b in list(comps):
            comps.add((b, a))

        def transitively_related(x1, x2, comps, visited=None):
            if x1 == x2:
                return True
            if visited is None:
                visited = set()
            elif x1 in visited:
                return False
            visited.add(x1)
            for a, b in comps:
                if a == x1:
                    if transitively_related(b, x2, comps, visited):
                        return True
            return False

        # cache maps output vectors to the best known plan implementing them
        cache = {}

        # ecache maps output vectors to ONE known predicate implementing them
        ecache = {}

        # _OfSize[s] contains all interesting plans of size s
        exprsOfSize = [[], [], [], []]
        plansOfSize = [[], [], [], [], []]

        print "round 1"
        for f1 in self.fieldNames:
            for f2 in self.fieldNames:
                if f1 < f2 and transitively_related(f1, f2, comps):
                    for op in predicates.operators:
                        plan = plans.AllWhere(predicates.Compare(
                            predicates.Var(f1), op, predicates.Var(f2)))
                        yield consider(plan, plan.size())
        for b in (True, False):
            plan = plans.AllWhere(predicates.Bool(b))
            yield consider(plan, plan.size())

        for v in self.varNames:
            for f in self.fieldNames:
                if (v, f) in comps:
                    for op in predicates.operators:
                        registerExp(predicates.Compare(
                            predicates.Var(v), op, predicates.Var(f)))
        for b in (True, False):
            registerExp(predicates.Bool(b))

        roundsWithoutProgress = 0
        maxRoundsWithoutProgress = 4

        for size in xrange(2, maxSize + 1):
            # exprs
            while len(exprsOfSize) <= size:
                exprsOfSize.append([])
            # Since we have all operators and their negations, we will never
            # generate anything interesting involving Not.
            # for e in exprsOfSize[size-1]:
            #     registerExp(predicates.Not(e))
            for e1, e2 in pickToSum(exprsOfSize, exprsOfSize, size):
                registerExp(predicates.And(e1, e2))
                registerExp(predicates.Or(e1, e2))

            # plans
            while len(plansOfSize) <= size:
                plansOfSize.append([])
            self.productive = False
            print "round", size, "; cache={}/{max}; ecache={}/{max}".format(len(cache), len(ecache), max=2**len(examples))
            for plan in (plans.HashLookup(p, e) for p, e in pickToSum(plansOfSize, exprsOfSize, size)):
                yield consider(plan, size)
            for plan in (plans.BinarySearch(p, e) for p, e in pickToSum(plansOfSize, exprsOfSize, size)):
                yield consider(plan, size)
            for plan in (plans.Filter(p, e) for p, e in pickToSum(plansOfSize, exprsOfSize, size)):
                yield consider(plan, size)
            for plan in (ty(p1, p2) for ty in [plans.Intersect, plans.Union, plans.Concat] for p1, p2 in pickToSum(plansOfSize, plansOfSize, size)):
                yield consider(plan, size)
            if self.productive:
                roundsWithoutProgress = 0
                print "  productive: {}".format(self.productive)
            else:
                roundsWithoutProgress += 1
                if roundsWithoutProgress >= maxRoundsWithoutProgress and size > 6:
                    print "last {} rounds were not productive; stopping".format(roundsWithoutProgress)
                    yield "stop", None
