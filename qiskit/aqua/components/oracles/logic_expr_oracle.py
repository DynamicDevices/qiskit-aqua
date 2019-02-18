# -*- coding: utf-8 -*-

# Copyright 2018 IBM.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# =============================================================================
"""
The General Logic Expression-based Quantum Oracle.
"""

import logging

from pyeda.inter import espresso_exprs
from pyeda.boolalg.expr import AndOp, OrOp, expr
from qiskit import QuantumCircuit, QuantumRegister

from qiskit.aqua import AquaError
from qiskit.aqua.utils import CNF, DNF

from .oracle import Oracle

logger = logging.getLogger(__name__)


class LogicExpressionOracle(Oracle):

    CONFIGURATION = {
        'name': 'LogicExpression',
        'description': 'Logic Expression Oracle',
        'input_schema': {
            '$schema': 'http://json-schema.org/schema#',
            'id': 'logic_expr_oracle_schema',
            'type': 'object',
            'properties': {
                'logic_expr_str': {
                    'type': 'string',
                },
                "optimization_mode": {
                    "anyOf": [
                        {
                            "type": "null",
                        },
                        {
                            "type": "string",
                            "default": "espresso",
                            'oneOf': [
                                {
                                    'enum': [
                                        'espresso',
                                    ]
                                }
                            ]
                        }
                    ]
                },
                'mct_mode': {
                    'type': 'string',
                    'default': 'basic',
                    'oneOf': [
                        {
                            'enum': [
                                'basic',
                                'advanced',
                                'noancilla'
                            ]
                        }
                    ]
                },
            },
            'additionalProperties': False
        }
    }

    def __init__(self, logic_expr_str=None, optimization_mode=None, mct_mode='basic'):
        """
        Constructor.

        Args:
            logic_expr_str (str): The string representation of a general boolean logic expression.
            optimization_mode (string): Optimization mode to use for minimizing the circuit.
                Currently, besides no optimization if omitted, Aqua also supports an 'expresso' mode
                <https://en.wikipedia.org/wiki/Espresso_heuristic_logic_minimizer>
            mct_mode (str): The mode to use for building Multiple-Control Toffoli.
        """

        self.validate(locals())
        self._mct_mode = mct_mode
        self._optimization_mode = optimization_mode

        if logic_expr_str is None:
            raise ValueError('Missing input logic expression string.')

        raw_expr = expr(logic_expr_str)
        self._expr = raw_expr

        self._process_expr()

        super().__init__()

    @staticmethod
    def apply_offset_to_lits(raw_ast, offset):
        if raw_ast[0] == 'and' or raw_ast[0] == 'or':
            clauses = []
            for c in raw_ast[1:]:
                if c[0] == 'lit':
                    clauses.append(('lit', (c[1] - offset) if c[1] > 0 else (c[1] + offset)))
                elif (c[0] == 'or' or c[0] == 'and') and (raw_ast[0] != c[0]):
                    clause = []
                    for l in c[1:]:
                        clause.append(('lit', (l[1] - offset) if l[1] > 0 else (l[1] + offset)))
                    clauses.append((c[0], *clause))
                else:
                    raise AquaError('Unrecognized logic expression: {}'.format(raw_ast))
        elif raw_ast[0] == 'const':
            return raw_ast
        else:
            raise AquaError('Unrecognized root expression type: {}.'.format(raw_ast[0]))
        return (raw_ast[0], *clauses)

    def _process_expr(self):
        self._num_vars = self._expr.degree
        ast = self._expr.to_cnf().to_ast()

        ast = LogicExpressionOracle.apply_offset_to_lits(ast, min(self._expr.usupport) - 1)

        if self._optimization_mode is None:
            self._nf = CNF(ast, num_vars=self._num_vars)
        elif self._optimization_mode == 'espresso':
            expr_dnf = self._expr.to_dnf()
            if expr_dnf.is_zero() or expr_dnf.is_one():
                self._nf = CNF(('const', 0 if expr_dnf.is_zero() else 1), num_vars=self._num_vars)
            else:
                expr_dnf_m = espresso_exprs(expr_dnf)[0]
                expr_dnf_m_ast = LogicExpressionOracle.apply_offset_to_lits(
                    expr_dnf_m.to_ast(), min(expr_dnf_m.usupport) - 1
                )
                if isinstance(expr_dnf_m, AndOp):
                    self._nf = CNF(expr_dnf_m_ast, num_vars=self._num_vars)
                elif isinstance(expr_dnf_m, OrOp):
                    self._nf = DNF(expr_dnf_m_ast, num_vars=self._num_vars)
                else:
                    raise AquaError('Unexpected espresso optimization result expr: {}'.format(expr_dnf_m))

    @property
    def variable_register(self):
        return self._variable_register

    @property
    def ancillary_register(self):
        return self._ancillary_register

    @property
    def output_register(self):
        return self._output_register

    def construct_circuit(self):
        if self._circuit is None:
            if self._nf is not None:
                self._circuit = self._nf.construct_circuit(mct_mode=self._mct_mode)
                self._variable_register = self._nf.variable_register
                self._output_register = self._nf.output_register
                self._ancillary_register = self._nf.ancillary_register
            else:
                self._variable_register = QuantumRegister(self._num_vars, name='v')
                self._output_register = QuantumRegister(1, name='o')
                self._ancillary_register = None
                self._circuit = QuantumCircuit(self._variable_register, self._output_register)
        return self._circuit

    def evaluate_classically(self, measurement):
        assignment = [(var + 1) * (int(tf) * 2 - 1) for tf, var in zip(measurement[::-1], range(len(measurement)))]
        if self._expr.is_zero():
            found = False
        elif self._expr.is_one():
            found = True
        else:
            prime_implicants = self._expr.complete_sum()
            if isinstance(prime_implicants, AndOp):
                prime_implicants_ast = LogicExpressionOracle.apply_offset_to_lits(
                    prime_implicants.to_ast(), min(prime_implicants.usupport) - 1
                )
                sols = [[l[1] for l in prime_implicants_ast[1:]]]
            elif isinstance(prime_implicants, OrOp):
                expr_complete_sum = self._expr.complete_sum()
                complete_sum_ast = LogicExpressionOracle.apply_offset_to_lits(
                    expr_complete_sum.to_ast(), min(expr_complete_sum.usupport) - 1
                )
                sols =[[l[1] for l in c[1:]] for c in complete_sum_ast[1:]]
            else:
                raise AquaError('Unexpected solution: {}'.format(prime_implicants))
            found = assignment in sols
        return found, assignment
