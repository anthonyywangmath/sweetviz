from typing import Union, List, Tuple
import os
import time
import pandas as pd
from numpy import isnan
from tqdm.auto import tqdm

from sweetviz.sv_types import NumWithPercent, FeatureToProcess, FeatureType
import sweetviz.from_dython as associations
import sweetviz.series_analyzer as sa
import sweetviz.utils as su
from sweetviz.graph_associations import GraphAssoc
from sweetviz.graph_associations import CORRELATION_ERROR
from sweetviz.graph_associations import CORRELATION_IDENTICAL
from sweetviz.graph_legend import GraphLegend
from sweetviz.config import config
import sweetviz.sv_html as sv_html
from sweetviz.feature_config import FeatureConfig
import webbrowser


class DataframeReport:
    def __init__(self,
                 source: Union[pd.DataFrame, Tuple[pd.DataFrame, str]],
                 target_feature_name: str = None,
                 compare: Union[pd.DataFrame, Tuple[pd.DataFrame, str]] = None,
                 pairwise_analysis: str = 'auto',
                 fc: FeatureConfig = None):
        pairwise_analysis = pairwise_analysis.lower()
        if pairwise_analysis not in ["on", "auto", "off"]:
            raise ValueError('"pairwise_analysis" parameter should be one of: "on", "auto", "off"')

        sv_html.load_layout_globals_from_config()

        self._jupyter_html = ""
        self._page_html = ""
        self._features = dict()
        self.compare_name = None
        self._target = None
        self.test_mode = False
        if fc is None:
            fc = FeatureConfig()

        # Associations: _associations[FEATURE][GIVES INFORMATION ABOUT THIS FEATURE]
        self._associations = dict()
        self._associations_compare = dict()
        self._association_graphs = dict()
        self._association_graphs_compare = dict()

        # Handle source and compare dataframes and names
        if type(source) == pd.DataFrame:
            source_df = source
            self.source_name = "DataFrame"
        elif type(source) == list:
            if len(source) != 2:
                raise ValueError('"source" parameter should either be a string or a list of 2 elements: [dataframe, "Name"].')
            source_df = source[0]
            self.source_name = source[1]
        else:
            raise ValueError('"source" parameter should either be a string or a list of 2 elements: [dataframe, "Name"].')
        if len(su.get_duplicate_cols(source_df)) > 0:
            raise ValueError('Duplicate column names detected in "source"; this is not supported.')

        all_source_names = [cur_name for cur_name, cur_series in source_df.iteritems()]
        if compare is None:
            compare_df = None
            self.compare_name = None
            all_compare_names = list()
        elif type(compare) == pd.DataFrame:
            compare_df = compare
            self.compare_name = "Compared"
            all_compare_names = [cur_name for cur_name, cur_series in compare_df.iteritems()]
        elif type(compare) == list:
            if len(compare) != 2:
                raise ValueError('"compare" parameter should either be a string or a list of 2 elements: [dataframe, "Name"].')
            compare_df = compare[0]
            self.compare_name = compare[1]
            all_compare_names = [cur_name for cur_name, cur_series in compare_df.iteritems()]
        else:
            raise ValueError('"compare" parameter should either be a string or a list of 2 elements: [dataframe, "Name"].')

        # Validate some params
        if compare_df is not None and len(su.get_duplicate_cols(compare_df)) > 0:
            raise ValueError('Duplicate column names detected in "compare"; this is not supported.')

        if target_feature_name in fc.skip:
            raise ValueError(f'"{target_feature_name}" was also specified as "skip". Target cannot be skipped.')

        for key in fc.get_all_mentioned_features():
            if key not in all_source_names:
                raise ValueError(f'"{key}" was specified in "feature_config" but is not found in source dataframe (watch case-sensitivity?).')

        # Find Features and Target (FILTER SKIPPED)
        filtered_series_names_in_source = [cur_name for cur_name, cur_series in source_df.iteritems()
                                           if cur_name not in fc.skip]
        for skipped in fc.skip:
            if skipped not in all_source_names and skipped not in all_compare_names:
                raise ValueError(f'"{skipped}" was marked as "skip" but is not in any provided dataframe (watch case-sensitivity?).')

        # Progress bar setup
        ratio_progress_of_df_summary_vs_feature = 1.0
        number_features = len(filtered_series_names_in_source)
        exponential_checks = number_features * number_features
        progress_chunks = ratio_progress_of_df_summary_vs_feature \
                            + number_features + (0 if target_feature_name is not None else 0)

        self.progress_bar = tqdm(total=progress_chunks, bar_format= \
                '{desc:42}|{bar}| [{percentage:3.0f}%]   {elapsed} -> ({remaining} left)', \
                ascii=False, dynamic_ncols=True)

        # self.progress_bar = tqdm(total=progress_chunks, bar_format= \
        #         '{desc:35}|{bar}| [{percentage:3.0f}%]   {elapsed}  -> ({remaining} left)', \
        #         ascii=False, ncols=90)
        #
        # Summarize dataframe
        self.progress_bar.set_description_str("[Summarizing dataframe]")
        self.summary_source = dict()
        self.summarize_dataframe(source_df, self.source_name, self.summary_source, fc.skip)
        if target_feature_name:
            self.summary_source["num_columns"] = self.summary_source["num_columns"] - 1
        if compare_df is not None:
            self.summary_compare = dict()
            self.summarize_dataframe(compare_df, self.compare_name, self.summary_compare, fc.skip)
            if target_feature_name:
                if target_feature_name in compare_df.columns:
                    self.summary_compare["num_columns"] = self.summary_compare["num_columns"] - 1
        else:
            self.summary_compare = None
        self.progress_bar.update(ratio_progress_of_df_summary_vs_feature)

        self.num_summaries = number_features

        # Association check
        if pairwise_analysis == 'auto' and \
                number_features > config["Processing"].getint("association_auto_threshold"):
            print(f"PAIRWISE CALCULATION LENGTH WARNING: There are {number_features} features in "
                  f"this dataframe and the "
                  f"'pairwise_analysis' parameter is set to 'auto'.\nPairwise analysis is exponential in "
                  f"length: {number_features} features will cause ~"
                  f"{number_features * number_features} pairs to be "
                  f"evaluated, which could take a long time.\n\nYou must call the function with the "
                  f"parameter pairwise_analysis='on' or 'off' to explicitly select desired behavior."
                  )
            self.progress_bar.close()
            return

        # Validate and process TARGET
        target_to_process = None
        target_type = None
        if target_feature_name:
            self.progress_bar.set_description_str(f"Feature: {target_feature_name} (TARGET)")
            targets_found = [item for item in filtered_series_names_in_source
                             if item == target_feature_name]
            if len(targets_found) == 0:
                raise KeyError(f"Feature '{target_feature_name}' was "
                               f"specified as TARGET, but is NOT FOUND in "
                               f"the dataframe (watch case-sensitivity?).")
            compare_target_series = None
            if compare_df is not None:
                if target_feature_name in compare_df.columns:
                    compare_target_series = compare_df[target_feature_name]

            # TARGET processed HERE with COMPARE if present
            target_to_process = FeatureToProcess(-1, source_df[targets_found[0]], compare_target_series,
                                                 None, None, fc.get_predetermined_type(targets_found[0]))
            self._target = sa.analyze_feature_to_dictionary(target_to_process)
            filtered_series_names_in_source.remove(targets_found[0])
            target_type = self._target["type"]
            self.progress_bar.update(1)

        # Set final target series and sanitize targets (e.g. bool->truly bool)
        source_target_series = None
        compare_target_series = None
        if target_feature_name:
            if target_feature_name not in source_df.columns:
                raise ValueError
            if self._target["type"] == sa.FeatureType.TYPE_BOOL:
                source_target_series = self.get_sanitized_bool_series(source_df[target_feature_name])
            else:
                source_target_series = source_df[target_feature_name]

            if compare_df is not None:
                if target_feature_name in compare_df.columns:
                    if self._target["type"] == sa.FeatureType.TYPE_BOOL:
                        compare_target_series = self.get_sanitized_bool_series(compare_df[
                                                                                   target_feature_name])
                    else:
                        compare_target_series = compare_df[target_feature_name]

        # Create list of features to process
        features_to_process = []
        for cur_series_name, cur_order_index in zip(filtered_series_names_in_source,
                                                 range(0, len(filtered_series_names_in_source))):
            # TODO: BETTER HANDLING OF DIFFERENT COLUMNS IN SOURCE/COMPARE
            if compare_df is not None and cur_series_name in \
                    compare_df.columns:
                this_feat = FeatureToProcess(cur_order_index,
                                             source_df[cur_series_name],
                                             compare_df[cur_series_name],
                                             source_target_series,
                                             compare_target_series,
                                             fc.get_predetermined_type(cur_series_name),
                                             target_type)
            else:
                this_feat = FeatureToProcess(cur_order_index,
                                             source_df[cur_series_name],
                                             None,
                                             source_target_series,
                                             None,
                                             fc.get_predetermined_type(cur_series_name),
                                             target_type)
            features_to_process.append(this_feat)


        # Process columns -> features
        self.run_id = hex(int(time.time()))[2:] + "_" # removes the decimals
        # self.temp_folder = config["Files"].get("temp_folder")
        # os.makedirs(os.path.normpath(self.temp_folder), exist_ok=True)

        for f in features_to_process:
            # start = time.perf_counter()
            self.progress_bar.set_description_str(f"Feature: {f.source.name}")
            self._features[f.source.name] = sa.analyze_feature_to_dictionary(f)
            self.progress_bar.update(1)
            # print(f"DONE FEATURE------> {f.source.name}"
            #       f" {(time.perf_counter() - start):.2f}   {self._features[f.source.name]['type']}")
        # self.progress_bar.set_description_str('[FEATURES DONE]')
        # self.progress_bar.close()

        # Wrap up summary
        self.summarize_category_types(source_df, self.summary_source, fc.skip)
        if compare is not None:
            self.summarize_category_types(compare_df, self.summary_compare, fc.skip)
        self.dataframe_summary_html = sv_html.generate_html_dataframe_summary(self)

        self.graph_legend = GraphLegend(self)

        # Process all associations
        # ----------------------------------------------------
        # Put target first
        if target_to_process is not None:
            features_to_process.insert(0,target_to_process)

        if pairwise_analysis.lower() != 'off':
            self.progress_bar.reset(total=len(features_to_process))
            self.progress_bar.set_description_str("[Step 2/3] Processing Pairwise Features")
            self.process_associations(features_to_process, source_target_series, compare_target_series)

            self.progress_bar.reset(total=1)
            self.progress_bar.set_description_str("[Step 3/3] Generating associations graph")
            self._association_graphs["all"] = GraphAssoc(self, "all", self._associations)
            self._association_graphs_compare["all"] = GraphAssoc(self, "all", self._associations_compare)
            self.associations_html_source = sv_html.generate_html_associations(self, "source")
            self.associations_html_compare = sv_html.generate_html_associations(self, "compare")
            self.progress_bar.set_description_str("Done! Use 'show' commands to display/save")
            self.progress_bar.update(1)
            self.progress_bar.close()
        else:
            self._associations = None
            self._associations_compare = None
            self.associations_html_source = None
            self.associations_html_compare = None
        return

    def __getitem__(self, key):
        # Can also access target
        if key in self._features.keys():
            return self._features[key]
        elif self._target is not None and key == self._target["name"]:
            return self._target
        else:
            return None

    def __setitem__(self, key, value):
        self._features[key] = value

    # OUTPUT
    # ----------------------------------------------------------------------------------------------
    def show_html(self, filepath='SWEETVIZ_REPORT.html', open_browser=True, layout='widescreen'):
        sv_html.load_layout_globals_from_config()
        self.page_layout = layout
        sv_html.set_summary_positions(self)
        sv_html.generate_html_detail(self)
        self._page_html = sv_html.generate_html_dataframe_page(self)

        f = open(filepath, 'w', encoding="utf-8")
        f.write(self._page_html)
        f.close()

        if open_browser:
            print(f"Report {filepath} was generated! NOTEBOOK/COLAB USERS: the web browser MAY not pop up, regardless, the report IS saved in your notebook/colab files.")
            # Not sure how to work around this: not fatal but annoying...Notebook/colab
            # https://bugs.python.org/issue5993
            webbrowser.open('file://' + os.path.realpath(filepath))
        else:
            print(f"Report {filepath} was generated!")

    @staticmethod
    def get_predetermined_type(name: str,
                               feature_predetermined_types: dict):
        if feature_predetermined_types is None:
            return sa.FeatureType.TYPE_UNSUPPORTED
        return sa.FeatureType.TYPE_UNSUPPORTED

    @staticmethod
    def sanitize_bool(value) -> bool:
        if value is bool:
            return value
        elif isinstance(value, str):
            return value.lower() in ['true', '1', 't', 'y', 'yes', '1.0']
        elif isinstance(value, float) or isinstance(value, int):
            return bool(value)
        return False

    @staticmethod
    def get_sanitized_bool_series(source: pd.Series) -> pd.Series:
        return source.map(DataframeReport.sanitize_bool, na_action='ignore')

    def get_target_type(self) -> FeatureType:
        if self._target is None:
            return None
        return self._target["type"]

    def get_type(self, feature_name: str) -> FeatureType:
        if self._features.get(feature_name) is None:
            if self._target["name"] == feature_name:
                return self._target["type"]
            else:
                return None
        return self._features[feature_name].get("type")

    def summarize_dataframe(self, source: pd.DataFrame, name: str, target_dict: dict, skip: List[str]):
        target_dict["name"] = name
        target_dict["num_rows"] = len(source)
        target_dict["num_columns"] = len(source.columns)
        target_dict["num_skipped_columns"] = len(source.columns) - len([x for x in source.columns if x not in skip])

        target_dict["memory_total"] = source.memory_usage(index=True, deep=True).sum()
        target_dict["memory_single_row"] = \
            float(target_dict["memory_total"]) / target_dict["num_rows"]

        target_dict["duplicates"] = NumWithPercent(sum(source.duplicated()), len(source))

    def summarize_category_types(self, source: pd.DataFrame, target_dict: dict, skip: List[str]):
        target_dict["num_cat"] = len([x for x in self._features.values()
                                        if (x["type"] == FeatureType.TYPE_CAT or x["type"] == FeatureType.TYPE_BOOL)
                                            and x["name"] not in skip and x["name"] in source])
        target_dict["num_numerical"] = len([x for x in self._features.values()
                                                    if x["type"] == FeatureType.TYPE_NUM and x["name"] not in skip \
                                                        and x["name"] in source])
        target_dict["num_text"] = len([x for x in self._features.values()
                                               if x["type"] == FeatureType.TYPE_TEXT and x["name"] not in skip \
                                                    and x["name"] in source])
        return

    def get_what_influences_me(self, feature_name: str) -> dict:
        influenced = dict()
        for cur_name, cur_associations in self._associations.items():
            if cur_name == feature_name:
                continue
            influence = cur_associations.get(feature_name)
            if influence is not None:
                influenced[cur_name] = influence
        return influenced

    def process_associations(self, features_to_process: List[FeatureToProcess], source_target_series,
            compare_target_series):

        def mirror_association(association_dict, feature_name, other_name, value):
            if other_name not in association_dict.keys():
                association_dict[other_name] = dict()
            other_dict = association_dict[other_name]
            if feature_name not in other_dict.keys():
                other_dict[feature_name] = value

        for feature in features_to_process:
            feature_name = feature.source.name
            if feature_name not in self._associations.keys():
                self._associations[feature_name] = dict()

            cur_associations = self._associations[feature_name]
            if feature.compare is not None:
                if feature_name not in self._associations_compare.keys():
                    self._associations_compare[feature_name] = dict()
                cur_associations_compare = self._associations_compare[feature_name]
            else:
                cur_associations_compare = None

            for other in features_to_process:
            # for other in [of for of in features_to_process if of.source.name != feature_name]:
                process_compare = cur_associations_compare is not None and other.compare is not None
                # if other.source.name in cur_associations.keys():
                #     print(f"Skipping {feature_name} {other.source.name}")
                #     continue
                if other.source.name == feature_name:
                    cur_associations[other.source.name] = 0.0
                    mirror_association(self._associations, feature_name, other.source.name, 0.0)
                    if process_compare:
                        cur_associations_compare[other.source.name] = 0.0
                        mirror_association(self._associations_compare, feature_name, other.source.name, 0.0)
                    continue

                if self[feature_name]["type"] == FeatureType.TYPE_CAT or \
                    self[feature_name]["type"] == FeatureType.TYPE_BOOL:
                    # CAT/BOOL source
                    # ------------------------------------
                    if self[other.source.name]["type"] == FeatureType.TYPE_CAT or \
                            self[other.source.name]["type"] == FeatureType.TYPE_BOOL:
                        # CAT-CAT
                        cur_associations[other.source.name] = \
                            associations.theils_u(feature.source, other.source)
                        if process_compare:
                            cur_associations_compare[other.source.name] = \
                                associations.theils_u(feature.compare, other.compare)
                    elif self[other.source.name]["type"] == FeatureType.TYPE_NUM:
                        # CAT-NUM
                        # This handles cat-num, then mirrors so no need to process num-cat separately
                        # (symmetrical relationship)
                        cur_associations[other.source.name] = \
                            associations.correlation_ratio(feature.source, other.source)
                        mirror_association(self._associations, feature_name, other.source.name, \
                                           cur_associations[other.source.name])
                        if process_compare:
                            cur_associations_compare[other.source.name] = \
                                associations.correlation_ratio(feature.compare, other.compare)
                            mirror_association(self._associations_compare, feature_name, other.source.name, \
                                               cur_associations_compare[other.source.name])

                elif self[feature_name]["type"] == FeatureType.TYPE_NUM:
                    # NUM source
                    # ------------------------------------
                    if self[other.source.name]["type"] == FeatureType.TYPE_NUM:
                        # NUM-NUM
                        cur_associations[other.source.name] = \
                            feature.source.corr(other.source, method='pearson')
                        # TODO: display correlation error better in graph!
                        if isnan(cur_associations[other.source.name]):
                            if feature.source.equals(other.source):
                                cur_associations[other.source.name] = CORRELATION_IDENTICAL
                            else:
                                # ERROR may occur if Nan's in one match values in other, and vice-versa
                                cur_associations[other.source.name] = CORRELATION_ERROR
                        mirror_association(self._associations, feature_name, other.source.name, \
                                           cur_associations[other.source.name])
                        if process_compare:
                            cur_associations_compare[other.source.name] = \
                                feature.compare.corr(other.compare, method='pearson')
                            # TODO: display correlation error better in graph!
                            if isnan(cur_associations_compare[other.source.name]):
                                if feature.compare.equals(other.compare):
                                    cur_associations_compare[other.source.name] = CORRELATION_IDENTICAL
                                else:
                                    # ERROR may occur if Nan's in one match values in other, and vice-versa
                                    cur_associations_compare[other.source.name] = CORRELATION_ERROR
                            mirror_association(self._associations_compare, feature_name, other.source.name, \
                                               cur_associations_compare[other.source.name])
            self.progress_bar.update(1)
