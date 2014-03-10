"""
Created on 2013.06.02

@author: Giovanni Cannata

Copyright 2013 Giovanni Cannata

This file is part of python3-ldap.

python3-ldap is free software: you can redistribute it and/or modify
it under the terms of the GNU Lesser General Public License as published
by the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

python3-ldap is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Lesser General Public License for more details.

You should have received a copy of the GNU Lesser General Public License
along with python3-ldap in the COPYING and COPYING.LESSER files.
If not, see <http://www.gnu.org/licenses/>.
"""

from string import whitespace
from os import linesep

from ldap3 import SEARCH_NEVER_DEREFERENCE_ALIASES, SEARCH_SCOPE_BASE_OBJECT, SEARCH_SCOPE_SINGLE_LEVEL, SEARCH_SCOPE_WHOLE_SUBTREE, SEARCH_DEREFERENCE_IN_SEARCHING, SEARCH_DEREFERENCE_FINDING_BASE_OBJECT, SEARCH_DEREFERENCE_ALWAYS, NO_ATTRIBUTES, \
    LDAPException
from ..protocol.rfc4511 import SearchRequest, LDAPDN, Scope, DerefAliases, Integer0ToMax, TypesOnly, AttributeSelection, Selector, EqualityMatch, AttributeDescription, AssertionValue, Filter, Not, And, Or, ApproxMatch, GreaterOrEqual, LessOrEqual, \
    ExtensibleMatch, Present, SubstringFilter, Substrings, Final, Initial, Any, ResultCode, Substring, MatchingRule, Type, MatchValue, DnAttributes
from ..operation.bind import referrals_to_list
from ..protocol.convert import ava_to_dict, attributes_to_list, search_refs_to_list


# SearchRequest ::= [APPLICATION 3] SEQUENCE {
#     baseObject      LDAPDN,
#     scope           ENUMERATED {
#         baseObject              (0),
#         singleLevel             (1),
#         wholeSubtree            (2),
#     ...  },
#     derefAliases    ENUMERATED {
#         neverDerefAliases       (0),
#         derefInSearching        (1),
#         derefFindingBaseObj     (2),
#         derefAlways             (3) },
#     sizeLimit       INTEGER (0 ..  maxInt),
#     timeLimit       INTEGER (0 ..  maxInt),
#     typesOnly       BOOLEAN,
#     filter          Filter,
#     attributes      AttributeSelection }

ROOT = 0
AND = 1
OR = 2
NOT = 3
MATCH_APPROX = 4
MATCH_GREATER_OR_EQUAL = 5
MATCH_LESS_OR_EQUAL = 6
MATCH_EXTENSIBLE = 7
MATCH_PRESENT = 8
MATCH_SUBSTRING = 9
MATCH_EQUAL = 10

SEARCH_OPEN = 20
SEARCH_OPEN_OR_CLOSE = 21
SEARCH_MATCH_OR_CLOSE = 22
SEARCH_MATCH_OR_CONTROL = 23

# simple cache for searchFilters
__memoizedFilters = dict()


class FilterNode():
    def __init__(self, tag=None, assertion=None):
        self.tag = tag
        self.parent = None
        self.assertion = assertion
        self.elements = []

    def append(self, filter_node):
        filter_node.parent = self
        self.elements.append(filter_node)
        return filter_node

    def __str__(self, pos=0):
        self.__repr__(pos)

    def __repr__(self, pos=0):
        nodetags = ['ROOT', 'AND', 'OR', 'NOT', 'MATCH_APPROX', 'MATCH_GREATER_OR_EQUAL', 'MATCH_LESS_OR_EQUAL', 'MATCH_EXTENSIBLE', 'MATCH_PRESENT', 'MATCH_SUBSTRING', 'MATCH_EQUAL']
        representation = ' ' * pos + 'tag: ' + nodetags[self.tag] + ' - assertion: ' + str(self.assertion)
        if self.elements:
            representation += ' - elements: ' + str(len(self.elements))
            for element in self.elements:
                representation += linesep
                representation += ' ' * pos + element.__repr__(pos + 2)

        return representation


def validate_assertion_value(value):
    value = value.strip()
    if r'\2a' in value:
        value = value.replace(r'\2a', '*')

    if r'\2A' in value:
        value = value.replace(r'\2A', '*')

    if r'\28' in value:
        value = value.replace(r'\28', '(')

    if r'\29' in value:
        value = value.replace(r'\29', ')')

    if r'\5c' in value:
        value = value.replace(r'\5c', '\\')

    if r'\5C' in value:
        value = value.replace(r'\5C', '\\')

    if r'\00' in value:
        value.replace(r'\00', chr(0))
    return value


def evaluate_match(match):
    match = match.strip()
    if '~=' in match:
        tag = MATCH_APPROX
        left_part, _, right_part = match.split('~=')
        assertion = {'attr': left_part.strip(), 'value': validate_assertion_value(right_part)}
    elif '>=' in match:
        tag = MATCH_GREATER_OR_EQUAL
        left_part, _, right_part = match.partition('>=')
        assertion = {'attr': left_part.strip(), 'value': validate_assertion_value(right_part)}
    elif '<=' in match:
        tag = MATCH_LESS_OR_EQUAL
        left_part, _, right_part = match.partition('<=')
        assertion = {'attr': left_part.strip(), 'value': validate_assertion_value(right_part)}
    elif ':=' in match:
        tag = MATCH_EXTENSIBLE
        left_part, _, right_part = match.partition(':=')
        extended_filter_list = left_part.split(':')
        matching_rule = None
        dn_attributes = None
        attribute_name = None
        if extended_filter_list[0] == '':  # extensible filter format [:dn]:matchingRule:=assertionValue
            if len(extended_filter_list) == 2 and extended_filter_list[1].lower().strip() != 'dn':
                matching_rule = validate_assertion_value(extended_filter_list[1])
            elif len(extended_filter_list) == 3 and extended_filter_list[1].lower().strip() == 'dn':
                dn_attributes = True
                matching_rule = validate_assertion_value(extended_filter_list[2])
            else:
                raise LDAPException('invalid extensible filter')
        elif len(extended_filter_list) <= 3:  # extensible filter format attr[:dn][:matchingRule]:=assertionValue
            if len(extended_filter_list) == 1:
                attribute_name = extended_filter_list[0]
            elif len(extended_filter_list) == 2:
                attribute_name = extended_filter_list[0]
                if extended_filter_list[1].lower().strip() == 'dn':
                    dn_attributes = True
                else:
                    matching_rule = validate_assertion_value(extended_filter_list[1])
            elif len(extended_filter_list) == 3 and extended_filter_list[1].lower().strip() == 'dn':
                attribute_name = extended_filter_list[0]
                dn_attributes = True
                matching_rule = validate_assertion_value(extended_filter_list[2])
            else:
                raise LDAPException('invalid extensible filter')

        if not attribute_name and not matching_rule:
            raise LDAPException('invalid extensible filter')

        assertion = {'attr': attribute_name.strip() if attribute_name else None, 'value': validate_assertion_value(right_part), 'matchingRule': matching_rule.strip() if matching_rule else None, 'dnAttributes': dn_attributes}
    elif match.endswith('=*'):
        tag = MATCH_PRESENT
        assertion = {'attr': match[:-2]}
    elif '=' in match and '*' in match:
        tag = MATCH_SUBSTRING
        left_part, _, right_part = match.partition('=')
        substrings = right_part.split('*')
        initial = validate_assertion_value(substrings[0]) if substrings[0] else None
        final = validate_assertion_value(substrings[-1]) if substrings[-1] else None
        any_string = [validate_assertion_value(substring) for substring in substrings[1:-1] if substring]
        assertion = {'attr': left_part, 'initial': initial, 'any': any_string, 'final': final}
    elif '=' in match:
        tag = MATCH_EQUAL
        left_part, _, right_part = match.partition('=')
        assertion = {'attr': left_part.strip(), 'value': validate_assertion_value(right_part)}
    else:
        raise LDAPException('invalid matching assertion')

    return FilterNode(tag, assertion)


def parse_filter(search_filter):
    search_filter = search_filter.strip()
    if search_filter and search_filter.count('(') == search_filter.count(')') and search_filter.startswith('(') and search_filter.endswith(')'):
        state = SEARCH_OPEN_OR_CLOSE
        root = FilterNode(ROOT)
        current_node = root
        start_pos = None
        skip_white_space = True
        just_closed = False
        for pos, c in enumerate(search_filter):
            if skip_white_space and c in whitespace:
                continue
            elif (state == SEARCH_OPEN or state == SEARCH_OPEN_OR_CLOSE) and c == '(':
                state = SEARCH_MATCH_OR_CONTROL
                just_closed = False
            elif state == SEARCH_MATCH_OR_CONTROL and c in '&!|':
                if c == '&':
                    current_node = current_node.append(FilterNode(AND))
                elif c == '|':
                    current_node = current_node.append(FilterNode(OR))
                elif c == '!':
                    current_node = current_node.append(FilterNode(NOT))
                state = SEARCH_OPEN
            elif (state == SEARCH_MATCH_OR_CLOSE or state == SEARCH_OPEN_OR_CLOSE) and c == ')':
                if just_closed:
                    current_node = current_node.parent
                else:
                    just_closed = True
                    skip_white_space = True
                    end_pos = pos
                    if start_pos:
                        if current_node.tag == NOT and len(current_node.elements) > 0:
                            raise LDAPException('Not clause in filter cannot be multiple')
                        current_node.append(evaluate_match(search_filter[start_pos:end_pos]))
                start_pos = None
                state = SEARCH_OPEN_OR_CLOSE
            elif (state == SEARCH_MATCH_OR_CLOSE or state == SEARCH_MATCH_OR_CONTROL) and c not in '()':
                skip_white_space = False
                if not start_pos:
                    start_pos = pos
                state = SEARCH_MATCH_OR_CLOSE
            else:
                raise LDAPException('malformed filter')
        if len(root.elements) != 1:
            raise LDAPException('missing boolean operator in filter')
        return root
    else:
        raise LDAPException('invalid filter')


def compile_filter(filter_node):
    compiled_filter = Filter()
    if filter_node.tag == AND:
        boolean_filter = And()
        pos = 0
        for element in filter_node.elements:
            boolean_filter[pos] = compile_filter(element)
            pos += 1
        compiled_filter['and'] = boolean_filter
    elif filter_node.tag == OR:
        boolean_filter = Or()
        pos = 0
        for element in filter_node.elements:
            boolean_filter[pos] = compile_filter(element)
            pos += 1
        compiled_filter['or'] = boolean_filter
    elif filter_node.tag == NOT:
        boolean_filter = Not()
        boolean_filter['innerNotFilter'] = compile_filter(filter_node.elements[0])
        compiled_filter['notFilter'] = boolean_filter
    elif filter_node.tag == MATCH_APPROX:
        matching_filter = ApproxMatch()
        matching_filter['attributeDesc'] = AttributeDescription(filter_node.assertion['attr'])
        matching_filter['assertionValue'] = AssertionValue(filter_node.assertion['value'])
        compiled_filter['approxMatch'] = matching_filter
    elif filter_node.tag == MATCH_GREATER_OR_EQUAL:
        matching_filter = GreaterOrEqual()
        matching_filter['attributeDesc'] = AttributeDescription(filter_node.assertion['attr'])
        matching_filter['assertionValue'] = AssertionValue(filter_node.assertion['value'])
        compiled_filter['greaterOrEqual'] = matching_filter
    elif filter_node.tag == MATCH_LESS_OR_EQUAL:
        matching_filter = LessOrEqual()
        matching_filter['attributeDesc'] = AttributeDescription(filter_node.assertion['attr'])
        matching_filter['assertionValue'] = AssertionValue(filter_node.assertion['value'])
        compiled_filter['lessOrEqual'] = matching_filter
    elif filter_node.tag == MATCH_EXTENSIBLE:
        matching_filter = ExtensibleMatch()
        if filter_node.assertion['matchingRule']:
            matching_filter['matchingRule'] = MatchingRule(filter_node.assertion['matchingRule'])
        if filter_node.assertion['attr']:
            matching_filter['type'] = Type(filter_node.assertion['attr'])
        matching_filter['matchValue'] = MatchValue(filter_node.assertion['value'])
        matching_filter['dnAttributes'] = DnAttributes(filter_node.assertion['dnAttributes'])
        compiled_filter['extensibleMatch'] = matching_filter
    elif filter_node.tag == MATCH_PRESENT:
        matching_filter = Present(AttributeDescription(filter_node.assertion['attr']))
        compiled_filter['present'] = matching_filter
    elif filter_node.tag == MATCH_SUBSTRING:
        matching_filter = SubstringFilter()
        matching_filter['type'] = AttributeDescription(filter_node.assertion['attr'])
        substrings = Substrings()
        pos = 0
        if filter_node.assertion['initial']:
            substrings[pos] = Substring().setComponentByName('initial', Initial(filter_node.assertion['initial']))
            pos += 1
        if filter_node.assertion['any']:
            for substring in filter_node.assertion['any']:
                substrings[pos] = Substring().setComponentByName('any', Any(substring))
                pos += 1
        if filter_node.assertion['final']:
            substrings[pos] = Substring().setComponentByName('final', Final(filter_node.assertion['final']))
        matching_filter['substrings'] = substrings
        compiled_filter['substringFilter'] = matching_filter
    elif filter_node.tag == MATCH_EQUAL:
        matching_filter = EqualityMatch()
        matching_filter['attributeDesc'] = AttributeDescription(filter_node.assertion['attr'])
        matching_filter['assertionValue'] = AssertionValue(filter_node.assertion['value'])
        compiled_filter.setComponentByName('equalityMatch', matching_filter)
    else:
        raise LDAPException('unknown filter')

    return compiled_filter


def build_filter(search_filter):
    return compile_filter(parse_filter(search_filter).elements[0])


def build_attribute_selection(attribute_list):
    attribute_selection = AttributeSelection()
    for index, attribute in enumerate(attribute_list):
        attribute_selection[index] = Selector(attribute)

    return attribute_selection


def search_operation(search_base, search_filter, search_scope, dereference_aliases, attributes, size_limit, time_limit, types_only):
    request = SearchRequest()
    request['baseObject'] = LDAPDN(search_base)

    if search_scope == SEARCH_SCOPE_BASE_OBJECT:
        request['scope'] = Scope('baseObject')
    elif search_scope == SEARCH_SCOPE_SINGLE_LEVEL:
        request['scope'] = Scope('singleLevel')
    elif search_scope == SEARCH_SCOPE_WHOLE_SUBTREE:
        request['scope'] = Scope('wholeSubtree')
    else:
        raise LDAPException('invalid scope type')

    if dereference_aliases == SEARCH_NEVER_DEREFERENCE_ALIASES:
        request['derefAliases'] = DerefAliases('neverDerefAliases')
    elif dereference_aliases == SEARCH_DEREFERENCE_IN_SEARCHING:
        request['derefAliases'] = DerefAliases('derefInSearching')
    elif dereference_aliases == SEARCH_DEREFERENCE_FINDING_BASE_OBJECT:
        request['derefAliases'] = DerefAliases('derefFindingBaseObj')
    elif dereference_aliases == SEARCH_DEREFERENCE_ALWAYS:
        request['derefAliases'] = DerefAliases('derefAlways')
    else:
        raise LDAPException('invalid dereference aliases type')

    request['sizeLimit'] = Integer0ToMax(size_limit)
    request['timeLimit'] = Integer0ToMax(time_limit)
    request['typesOnly'] = TypesOnly(True) if types_only else TypesOnly(False)
    request['filter'] = compile_filter(parse_filter(search_filter).elements[0])  # parse the searchFilter string and compile it starting from the root node

    if not isinstance(attributes, list):
        attributes = [NO_ATTRIBUTES]

    request['attributes'] = build_attribute_selection(attributes)

    return request


def decode_vals(vals):
    if vals:
        return [str(val) for val in vals if val]
    else:
        return None


def attributes_to_dict(attribute_list):
    attributes = dict()
    for attribute in attribute_list:
        attributes[str(attribute['type'])] = decode_vals(attribute['vals'])

    return attributes


def decode_raw_vals(vals):
    if vals:
        return [bytes(val) for val in vals]
    else:
        return None


def raw_attributes_to_dict(attribute_list):
    attributes = dict()
    for attribute in attribute_list:
        attributes[str(attribute['type'])] = decode_raw_vals(attribute['vals'])

    return attributes


def matching_rule_assertion_to_string(matching_rule_assertion):
    return str(matching_rule_assertion)


def filter_to_string(filter_object):
    filter_type = filter_object.getName()
    filter_string = '('
    if filter_type == 'and':
        filter_string += '&'
        for f in filter_object['and']:
            filter_string += filter_to_string(f)
    elif filter_type == 'or':
        filter_string += '!'
        for f in filter_object['or']:
            filter_string += filter_to_string(f)
    elif filter_type == 'notFilter':
        filter_string += '!' + filter_to_string(filter_object['notFilter']['innerNotFilter'])
    elif filter_type == 'equalityMatch':
        ava = ava_to_dict(filter_object['equalityMatch'])
        filter_string += ava['attribute'] + '=' + ava['value']
    elif filter_type == 'substringFilter':
        attribute = filter_object['substringFilter']['type']
        filter_string += str(attribute) + '='
        for substring in filter_object['substringFilter']['substrings']:
            if substring['initial']:
                filter_string += str(substring['initial']) + '*'
            elif substring['any']:
                filter_string += str(substring['any']) if filter_string.endswith('*') else '*' + str(substring['any'])
                filter_string += '*'
            elif substring['final']:
                filter_string += '*' + str(substring['final'])
    elif filter_type == 'greaterOrEqual':
        ava = ava_to_dict(filter_object['greaterOrEqual'])
        filter_string += ava['attribute'] + '>=' + ava['value']
    elif filter_type == 'lessOrEqual':
        ava = ava_to_dict(filter_object['lessOrEqual'])
        filter_string += ava['attribute'] + '<=' + ava['value']
    elif filter_type == 'present':
        filter_string += str(filter_object['present']) + '=*'
    elif filter_type == 'approxMatch':
        ava = ava_to_dict(filter_object['approxMatch'])
        filter_string += ava['attribute'] + '~=' + ava['value']
    elif filter_type == 'extensibleMatch':
        filter_string += matching_rule_assertion_to_string(filter_object['extensibleMatch'])
    else:
        raise LDAPException('error converting filter to string')

    filter_string += ')'
    return filter_string


def search_request_to_dict(request):
    return {'base': str(request['baseObject']), 'scope': int(request['scope']), 'dereferenceAlias': int(request['derefAliases']), 'sizeLimit': int(request['sizeLimit']), 'timeLimit': int(request['timeLimit']), 'typeOnly': bool(request['typesOnly']),
            'filter': filter_to_string(request['filter']), 'attributes': attributes_to_list(request['attributes'])}


def search_result_entry_response_to_dict(response):
    return {'dn': str(response['object']), 'attributes': attributes_to_dict(response['attributes']), 'raw_attributes': raw_attributes_to_dict(response['attributes'])}


def search_result_done_response_to_dict(response):
    return {'result': int(response[0]), 'description': ResultCode().getNamedValues().getName(response[0]), 'message': str(response['diagnosticMessage']), 'dn': str(response['matchedDN']), 'referrals': referrals_to_list(response['referral'])}


def search_result_reference_response_to_dict(response):
    return {'uri': search_refs_to_list(response)}
