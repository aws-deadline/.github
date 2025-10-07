#!/usr/bin/env python3
"""Shared configuration for all generation scripts."""

INTEGRATIONS = {
    'keyshot': {
        'repo': 'deadline-cloud-for-keyshot',
        'display_name': 'KeyShot Studio',
        'has_user_guide': True
    },
    'cinema-4d': {
        'repo': 'deadline-cloud-for-cinema-4d',
        'display_name': 'Maxon Cinema 4D',
        'has_user_guide': True
    },
    'maya': {
        'repo': 'deadline-cloud-for-maya',
        'display_name': 'Autodesk Maya',
        'has_user_guide': False
    },
    'blender': {
        'repo': 'deadline-cloud-for-blender',
        'display_name': 'Blender',
        'has_user_guide': False
    },
    'nuke': {
        'repo': 'deadline-cloud-for-nuke',
        'display_name': 'Foundry Nuke',
        'has_user_guide': False
    },
    'after-effects': {
        'repo': 'deadline-cloud-for-after-effects',
        'display_name': 'Adobe After Effects',
        'has_user_guide': False
    },
    '3ds-max': {
        'repo': 'deadline-cloud-for-3ds-max',
        'display_name': 'Autodesk 3ds Max',
        'has_user_guide': False
    },
    'houdini': {
        'repo': 'deadline-cloud-for-houdini',
        'display_name': 'SideFX Houdini',
        'has_user_guide': False
    },
    'unreal-engine': {
        'repo': 'deadline-cloud-for-unreal-engine',
        'display_name': 'Unreal Engine',
        'has_user_guide': False
    }
}
