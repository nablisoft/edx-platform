"""
Utilities to facilitate experimentation
"""

import hashlib
import re
import logging
from decimal import Decimal
from student.models import CourseEnrollment
from django_comment_common.models import Role
from django.utils.timezone import now
from lms.djangoapps.commerce.utils import EcommerceService
from course_modes.models import get_cosmetic_verified_display_price, format_course_price
from courseware.access import has_staff_access_to_preview_mode
from courseware.date_summary import verified_upgrade_deadline_link, verified_upgrade_link_is_valid
from xmodule.partitions.partitions_service import get_user_partition_groups, get_all_partitions_for_course
from opaque_keys.edx.keys import CourseKey
from opaque_keys import InvalidKeyError
from openedx.core.djangoapps.catalog.utils import get_programs
from openedx.core.djangoapps.waffle_utils import WaffleFlag, WaffleFlagNamespace


logger = logging.getLogger(__name__)


# TODO: clean up as part of REVEM-199 (START)
# .. feature_toggle_name: experiments.add_programs
# .. feature_toggle_type: flag
# .. feature_toggle_default: False
# .. feature_toggle_description: Toggle for adding the current course's program information to user metadata
# .. feature_toggle_category: experiments
# .. feature_toggle_use_cases: monitored_rollout
# .. feature_toggle_creation_date: 2019-2-25
# .. feature_toggle_expiration_date: None
# .. feature_toggle_warnings: None
# .. feature_toggle_tickets: REVEM-63, REVEM-198
# .. feature_toggle_status: supported
PROGRAM_INFO_FLAG = WaffleFlag(
    waffle_namespace=WaffleFlagNamespace(name=u'experiments'),
    flag_name=u'add_programs',
    flag_undefined_default=False
)

# .. feature_toggle_name: experiments.add_program_price
# .. feature_toggle_type: flag
# .. feature_toggle_default: False
# .. feature_toggle_description: Toggle for adding the current course's program price and sku information to user
#                                metadata
# .. feature_toggle_category: experiments
# .. feature_toggle_use_cases: monitored_rollout
# .. feature_toggle_creation_date: 2019-3-12
# .. feature_toggle_expiration_date: None
# .. feature_toggle_warnings: None
# .. feature_toggle_tickets: REVEM-118, REVEM-206
# .. feature_toggle_status: supported
PROGRAM_PRICE_FLAG = WaffleFlag(
    waffle_namespace=WaffleFlagNamespace(name=u'experiments'),
    flag_name=u'add_program_price',
    flag_undefined_default=False
)
# TODO: clean up as part of REVEM-199 (END)


def check_and_get_upgrade_link_and_date(user, enrollment=None, course=None):
    """
    For an authenticated user, return a link to allow them to upgrade
    in the specified course.

    Returns the upgrade link and upgrade deadline for a user in a given course given
    that the user is within the window to upgrade defined by our dynamic pacing feature;
    otherwise, returns None for both the link and date.
    """
    if enrollment is None and course is None:
        raise ValueError("Must specify either an enrollment or a course")

    if enrollment:
        if course is None:
            course = enrollment.course
        elif enrollment.course_id != course.id:
            raise ValueError(u"{} refers to a different course than {} which was supplied".format(
                enrollment, course
            ))

        if enrollment.user_id != user.id:
            raise ValueError(u"{} refers to a different user than {} which was supplied".format(
                enrollment, user
            ))

    if enrollment is None:
        enrollment = CourseEnrollment.get_enrollment(user, course.id)

    if user.is_authenticated and verified_upgrade_link_is_valid(enrollment):
        return (
            verified_upgrade_deadline_link(user, course),
            enrollment.upgrade_deadline
        )

    return (None, None)


# TODO: clean up as part of REVEM-199 (START)
def get_program_price_and_skus(courses):
    """
    Get the total program price and purchase skus from these courses in the program
    """
    program_price = 0
    skus = []

    for course in courses:
        course_price, course_sku = get_course_entitlement_price_and_sku(course)
        if course_price is not None and course_sku is not None:
            program_price = Decimal(program_price) + Decimal(course_price)
            skus.append(course_sku)

    if program_price <= 0:
        program_price = None
        skus = None
    else:
        program_price = format_course_price(program_price)
        program_price = unicode(program_price)

    return program_price, skus


def get_course_entitlement_price_and_sku(course):
    """
    Get the entitlement price and sku from this course.
    Try to get them from the first non-expired, verified entitlement that has a price and a sku. If that doesn't work,
    fall back to the first non-expired, verified course run that has a price and a sku.
    """
    for entitlement in course.get('entitlements', []):
        if entitlement.get('mode') == 'verified' and entitlement['price'] and entitlement['sku']:
            expires = entitlement.get('expires')
            if not expires or expires > now():
                return entitlement['price'], entitlement['sku']

    course_runs = course.get('course_runs', [])
    published_course_runs = [run for run in course_runs if run['status'] == 'published']
    for published_course_run in published_course_runs:
        for seat in published_course_run['seats']:
            if seat.get('type') == 'verified' and seat['price'] and seat['sku']:
                price = Decimal(seat.get('price'))
                return price, seat.get('sku')

    return None, None


def get_unenrolled_courses(courses, user_enrollments):
    """
    Given a list of courses and a list of user enrollments, return the courses in which the user is not enrolled.
    Depending on the enrollments that are passed in, this method can be used to determine the courses in a program in
    which the user has not yet enrolled or the courses in a program for which the user has not yet purchased a
    certificate.
    """
    # Get the enrollment course ids here, so we don't need to loop through them for every course run
    enrollment_course_ids = {enrollment.course_id for enrollment in user_enrollments}
    unenrolled_courses = []

    for course in courses:
        if not is_enrolled_in_course(course, enrollment_course_ids):
            unenrolled_courses.append(course)
    return unenrolled_courses


def is_enrolled_in_all_courses(courses, user_enrollments):
    """
    Determine if the user is enrolled in all of the courses
    """
    # Get the enrollment course ids here, so we don't need to loop through them for every course run
    enrollment_course_ids = {enrollment.course_id for enrollment in user_enrollments}

    for course in courses:
        if not is_enrolled_in_course(course, enrollment_course_ids):
            # User is not enrolled in this course, meaning they are not enrolled in all courses in the program
            return False
    # User is enrolled in all courses in the program
    return True


def is_enrolled_in_course(course, enrollment_course_ids):
    """
    Determine if the user is enrolled in this course
    """
    course_runs = course.get('course_runs')
    if course_runs:
        for course_run in course_runs:
            if is_enrolled_in_course_run(course_run, enrollment_course_ids):
                return True
    return False


def is_enrolled_in_course_run(course_run, enrollment_course_ids):
    """
    Determine if the user is enrolled in this course run
    """
    key = None
    try:
        key = course_run.get('key')
        course_run_key = CourseKey.from_string(key)
        return course_run_key in enrollment_course_ids
    except InvalidKeyError:
        logger.warn(
            u'Unable to determine if user was enrolled since the course key {} is invalid'.format(key)
        )
        return False  # Invalid course run key. Assume user is not enrolled.
# TODO: clean up as part of REVEM-199 (END)


def get_experiment_user_metadata_context(course, user):
    """
    Return a context dictionary with the keys used by the user_metadata.html.
    """
    enrollment_mode = None
    enrollment_time = None
    enrollment = None
    # TODO: clean up as part of REVO-28 (START)
    has_non_audit_enrollments = None
    # TODO: clean up as part of REVO-28 (END)
    # TODO: clean up as part of REVEM-199 (START)
    program_key = None
    # TODO: clean up as part of REVEM-199 (END)
    try:
        # TODO: clean up as part of REVO-28 (START)
        user_enrollments = CourseEnrollment.objects.select_related('course').filter(user_id=user.id)
        audit_enrollments = user_enrollments.filter(mode='audit')
        has_non_audit_enrollments = (len(audit_enrollments) != len(user_enrollments))
        # TODO: clean up as part of REVO-28 (END)
        # TODO: clean up as part of REVEM-199 (START)
        if PROGRAM_INFO_FLAG.is_enabled():
            programs = get_programs(course=course.id)
            if programs:
                # A course can be in multiple programs, but we're just grabbing the first one
                program = programs[0]
                complete_enrollment = False
                has_courses_left_to_purchase = False
                total_courses = None
                courses = program.get('courses')
                courses_left_to_purchase_price = None
                courses_left_to_purchase_url = None
                program_uuid = program.get('uuid')
                if courses is not None:
                    total_courses = len(courses)
                    complete_enrollment = is_enrolled_in_all_courses(courses, user_enrollments)

                    if PROGRAM_PRICE_FLAG.is_enabled():
                        # Get the price and purchase URL of the program courses the user has yet to purchase. Say a
                        # program has 3 courses (A, B and C), and the user previously purchased a certificate for A.
                        # The user is enrolled in audit mode for B. The "left to purchase price" should be the price of
                        # B+C.
                        non_audit_enrollments = [enrollment for enrollment in user_enrollments if enrollment not in
                                                 audit_enrollments]
                        courses_left_to_purchase = get_unenrolled_courses(courses, non_audit_enrollments)
                        if courses_left_to_purchase:
                            has_courses_left_to_purchase = True
                        courses_left_to_purchase_price, courses_left_to_purchase_skus = get_program_price_and_skus(
                            courses_left_to_purchase)
                        courses_left_to_purchase_url = EcommerceService().get_checkout_page_url(
                            *courses_left_to_purchase_skus, program_uuid=program_uuid)

                program_key = {
                    'uuid': program_uuid,
                    'title': program.get('title'),
                    'marketing_url': program.get('marketing_url'),
                    'total_courses': total_courses,
                    'complete_enrollment': complete_enrollment,
                    'has_courses_left_to_purchase': has_courses_left_to_purchase,
                    'courses_left_to_purchase_price': courses_left_to_purchase_price,
                    'courses_left_to_purchase_url': courses_left_to_purchase_url,
                }
        # TODO: clean up as part of REVEM-199 (END)
        enrollment = CourseEnrollment.objects.select_related(
            'course'
        ).get(user_id=user.id, course_id=course.id)
        if enrollment.is_active:
            enrollment_mode = enrollment.mode
            enrollment_time = enrollment.created
    except CourseEnrollment.DoesNotExist:
        pass  # Not enrolled, used the default None values

    # upgrade_link and upgrade_date should be None if user has passed their dynamic pacing deadline.
    upgrade_link, upgrade_date = check_and_get_upgrade_link_and_date(user, enrollment, course)
    has_staff_access = has_staff_access_to_preview_mode(user, course.id)
    forum_roles = []
    if user.is_authenticated:
        forum_roles = list(Role.objects.filter(users=user, course_id=course.id).values_list('name').distinct())

    # get user partition data
    if user.is_authenticated():
        partition_groups = get_all_partitions_for_course(course)
        user_partitions = get_user_partition_groups(course.id, partition_groups, user, 'name')
    else:
        user_partitions = {}

    return {
        'upgrade_link': upgrade_link,
        'upgrade_price': unicode(get_cosmetic_verified_display_price(course)),
        'enrollment_mode': enrollment_mode,
        'enrollment_time': enrollment_time,
        'pacing_type': 'self_paced' if course.self_paced else 'instructor_paced',
        'upgrade_deadline': upgrade_date,
        'course_key': course.id,
        'course_start': course.start,
        'course_end': course.end,
        'has_staff_access': has_staff_access,
        'forum_roles': forum_roles,
        'partition_groups': user_partitions,
        # TODO: clean up as part of REVO-28 (START)
        'has_non_audit_enrollments': has_non_audit_enrollments,
        # TODO: clean up as part of REVO-28 (END)
        # TODO: clean up as part of REVEM-199 (START)
        'program_key_fields': program_key,
        # TODO: clean up as part of REVEM-199 (END)
    }


#TODO START: Clean up REVEM-205
def get_experiment_dashboard_metadata_context(enrollments):
    """
    Given a list of enrollments return a dict of course ids with their prices.
    Utility function for experimental metadata. See experiments/dashboard_metadata.html.
    :param enrollments:
    :return: dict of courses: course price for dashboard metadata
    """
    return {str(enrollment.course): enrollment.course_price for enrollment in enrollments}
#TODO END: Clean up REVEM-205


def stable_bucketing_hash_group(group_name, group_count, username):
    """
    Return the bucket that a user should be in for a given stable bucketing assignment.

    This function has been verified to return the same values as the stable bucketing
    functions in javascript and the master experiments table.

    Arguments:
        group_name: The name of the grouping/experiment.
        group_count: How many groups to bucket users into.
        username: The username of the user being bucketed.
    """
    hasher = hashlib.md5()
    hasher.update(group_name.encode('utf-8'))
    hasher.update(username.encode('utf-8'))
    hash_str = hasher.hexdigest()

    return int(re.sub('[8-9a-f]', '1', re.sub('[0-7]', '0', hash_str)), 2) % group_count
